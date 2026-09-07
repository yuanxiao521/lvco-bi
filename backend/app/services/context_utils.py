"""上下文压缩工具：工具结果摘要化 + 历史消息压缩。

用途：控制注入 LLM 上下文的体积，降低 token 消耗、防止长结果撑爆上下文。

原则：
- error 结果不压缩（错误信息与 hint 必须完整回传给 LLM 用于自纠错）
- 成功结果保留结构（summary/columns/统计字段），只截断数据主体（rows/insights 等）
"""
from __future__ import annotations

import json
from typing import Any

from app.config import settings

_MAX_RESULT_CHARS = settings.RESULT_MAX_CHARS  # 单个工具结果注入上下文的最大字符数
_MAX_ROWS = settings.RESULT_MAX_ROWS            # rows 保留前 N 行


def compact_result_json(result_str: str, max_chars: int = _MAX_RESULT_CHARS) -> str:
    """把工具返回的 JSON 结果压缩成摘要，用于注入 LLM 上下文。

    - 输入不是 JSON 或较短时原样返回
    - error 结果完整保留（自纠错依赖错误与 hint）
    - 成功结果：rows 只留前 _MAX_ROWS 行；insights 等建议列表完整保留，
      仅受 max_chars 总字符上限兜底
    """
    if not result_str or len(result_str) <= max_chars:
        return result_str
    try:
        obj = json.loads(result_str)
    except Exception:
        return result_str[:max_chars] + f"\n…(已截断 {len(result_str) - max_chars} 字符)"
    if not isinstance(obj, dict):
        return result_str[:max_chars] + "…(已截断)"
    if "error" in obj:
        return result_str  # 错误完整保留，供 LLM 修复

    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k == "rows" and isinstance(v, list):
            out["rows"] = v[:_MAX_ROWS]
            out["rows_total"] = len(v)
            out["rows_truncated"] = len(v) > _MAX_ROWS
        else:
            out[k] = v  # insights 等建议列表完整保留，仅受 max_chars 兜底
    s = json.dumps(out, ensure_ascii=False, default=str)
    if len(s) > max_chars:
        s = s[:max_chars] + "…(已截断)"
    return s


def compress_history(messages: list[dict], keep: int = 0, max_chars: int = 0) -> list[dict]:
    """历史消息压缩：保留 system 首条 + 最近 keep 条；总长超限时把最早部分折叠为摘要行。

    返回：压缩后的消息列表（不修改入参）。
    """
    if not messages:
        return messages
    keep = keep or settings.CONTEXT_KEEP
    max_chars = max_chars or settings.CONTEXT_MAX_CHARS
    total = sum(len(str(m.get("content", ""))) for m in messages)
    if len(messages) <= keep and total <= max_chars:
        return messages

    head: list[dict] = [messages[0]] if messages and messages[0].get("role") == "system" else []
    rest = list(messages[len(head):])
    if len(rest) > keep:
        dropped = len(rest) - keep
        kept_tail = rest[-keep:]
        dropped_chars = total - sum(len(str(m.get("content", ""))) for m in head + kept_tail)
        digest: dict = {
            "role": "user",
            "content": f"（【系统提示】较早的 {dropped} 条对话共约 {dropped_chars} 字已省略，请基于后续对话继续。）",
        }
        rest = [digest] + kept_tail
    return head + rest


# ── 智能压缩：窗口保留 + LLM 摘要 ─────────────────────────────────────────

COMPRESSION_MARKER = "【压缩摘要】"
_COMPRESSION_MARKER = COMPRESSION_MARKER


def _count_rounds_since_marker(messages: list[dict]) -> int:
    """从上次压缩标记点之后，数完整对话轮次（user->assistant 对）。"""
    marker_idx = -1
    for i, m in enumerate(messages):
        role = m.get("role", "")
        content = str(m.get("content", ""))
        if role == "assistant" and content.startswith(_COMPRESSION_MARKER):
            marker_idx = i
    start = marker_idx + 1
    rounds = 0
    for i in range(start, len(messages)):
        if messages[i].get("role") == "user":
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                rounds += 1
    return rounds


def _has_no_tool_msgs(messages: list[dict]) -> bool:
    return not any(m.get("role") == "tool" for m in messages)


def _find_keep_boundary(messages: list[dict], keep_rounds: int = 2) -> int:
    """从后往前找保留 keep_rounds 轮完整对话的边界索引。"""
    found = 0
    boundary = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant" and found < keep_rounds:
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "user":
                    found += 1
                    boundary = j
                    break
        if found >= keep_rounds:
            break
    return boundary


def _extract_tool_call_pairs(messages: list[dict]) -> list[dict]:
    """提取 tool_call + tool 对，用于 LLM 压缩成摘要。"""
    pairs = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tc_msg = m
            tool_results = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_results.append(messages[j])
                j += 1
            pairs.append({"tool_calls": tc_msg, "tool_results": tool_results})
            i = j
        else:
            i += 1
    return pairs


async def smart_compress_history(
    messages: list[dict],
    llm,
    min_rounds: int = 0,
    keep_rounds: int = 0,
    max_chars: int = 0,
) -> list[dict]:
    """智能压缩：保留最后 keep_rounds 轮完整上下文，将更早轮次的 tool 结果（不含 tool_call）
    压缩为 LLM 生成的结构化摘要。

    - tool_call 消息保留不动（参数细节供失败归因 + 反思重试）
    - tool 结果消息替换为摘要
    - 触发条件：距上次压缩 >= min_rounds 轮完整对话。
    - 压缩后若总长仍 > max_chars，回退到 truncation 截断兜底。

    默认值从 settings 读取：
    min_rounds=CONTEXT_MIN_ROUNDS(3), keep_rounds=CONTEXT_KEEP_ROUNDS(3), max_chars=CONTEXT_MAX_CHARS(150000)
    """
    if not messages or len(messages) < 4:
        return messages
    if _has_no_tool_msgs(messages):
        return messages

    min_rounds = min_rounds or settings.CONTEXT_MIN_ROUNDS
    keep_rounds = keep_rounds or settings.CONTEXT_KEEP_ROUNDS
    max_chars = max_chars or settings.CONTEXT_MAX_CHARS
    rounds = _count_rounds_since_marker(messages)
    if rounds < min_rounds:
        return messages

    boundary = _find_keep_boundary(messages, keep_rounds)
    if boundary <= 0:
        return messages

    to_compress = messages[:boundary]
    to_keep = messages[boundary:]

    pairs = _extract_tool_call_pairs(to_compress)
    if not pairs:
        return messages

    pair_lines = []
    for i, p in enumerate(pairs):
        tc = p["tool_calls"]
        tcs = tc.get("tool_calls") or []
        tool_names = [t.get("function", {}).get("name", "?") for t in tcs]
        trs = p["tool_results"]
        tr_contents = [str(t.get("content", ""))[:200] for t in trs]
        pair_lines.append(f"工具调用 {i + 1}: {', '.join(tool_names)}\n结果预览: {', '.join(tr_contents)}")

    prompt = (
        "以下是一次数据分析对话中工具调用的记录。请提取关键信息，生成一段简洁的摘要（200 字以内）"
        "，保留所有关键数值和统计结果（如具体金额、数量、排名、百分比）。\n"
        "不要包含工具名和参数细节，只输出最终结果。\n\n"
        + "\n---\n".join(pair_lines)
    )

    try:
        summary = await llm.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        if not summary or not summary.strip():
            return messages
        summary = summary.strip()
    except Exception:
        return messages

    # 遍历压缩段：保留 tool_call（参数细节），跳过 tool 结果，追加摘要
    compressed = []
    i = 0
    pair_idx = 0
    while i < len(to_compress):
        m = to_compress[i]
        if m.get("role") == "assistant" and m.get("tool_calls") and pair_idx < len(pairs):
            compressed.append(m)  # 保留 tool_call 消息（参数细节供归因/重试）
            i += 1
            while i < len(to_compress) and to_compress[i].get("role") == "tool":
                i += 1  # 跳过 tool 结果
            compressed.append({"role": "assistant", "content": f"{_COMPRESSION_MARKER}{summary}"})
            pair_idx += 1
        else:
            compressed.append(m)
            i += 1

    result = compressed + to_keep

    # 阈值兜底：LLM 压缩后总长仍超限 → 回退到 truncation 截断
    total = sum(len(str(m.get("content", ""))) for m in result)
    if total > max_chars:
        result = compress_history(result, keep=keep_rounds * 4, max_chars=max_chars)

    return result


def count_rounds_since_marker(messages: list[dict]) -> int:
    """公开封装：统计距上次压缩标记后的完整对话轮次（用于记录覆盖轮次）。"""
    return _count_rounds_since_marker(messages)


def extract_compressed_digest(messages: list[dict]) -> str:
    """从消息列表中提取所有压缩摘要标记内容，拼接为一条跨轮记忆文本。

    smart_compress_history 会把较早轮次折叠成以 COMPRESSION_MARKER 开头的
    assistant 消息；此函数取回这些摘要，供路由持久化到会话级记忆（ai_memories）。
    返回 "" 表示没有可用的压缩摘要。
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "")
        content = str(m.get("content", ""))
        if role == "assistant" and content.startswith(COMPRESSION_MARKER):
            text = content[len(COMPRESSION_MARKER):].strip()
            if text:
                parts.append(text)
    return "\n".join(parts)