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

# 字符 ↔ token 换算（与 services/context/budgets.py 同系数；本文件是字符口径的 legacy 层）
_TOKENS_PER_CHAR_MIXED = 0.6
_TOKENS_PER_CHAR_JSON = 0.4


def _chars_for_tokens(tokens: int, tokens_per_char: float) -> int:
    return int(round(max(0, tokens) / tokens_per_char)) if tokens_per_char > 0 else 0


_MAX_RESULT_CHARS = _chars_for_tokens(
    int(getattr(settings, "TOOL_RESULT_MAX_TOKENS", 800) or 800), _TOKENS_PER_CHAR_JSON
)   # 单个工具结果注入上下文的最大字符数（token 口径反推）
_MAX_ROWS = settings.RESULT_MAX_ROWS            # rows 保留前 N 行


# ── 上下文装配通用工具 ──────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """粗略 token 估算（中英混排），只用于预算判断与可观测，不用于计费。

    规则：CJK/全角字符按 1 字 ≈ 1 token；其余按 4 字符 ≈ 1 token。整体偏保守（宁多不少）。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        code = ord(ch)
        if (
            0x4E00 <= code <= 0x9FFF      # CJK 统一汉字
            or 0x3000 <= code <= 0x303F   # CJK 标点
            or 0xFF00 <= code <= 0xFFEF   # 全角字符
            or 0x3040 <= code <= 0x30FF   # 日文假名（混排时同样 1 字 1 token 量级）
        ):
            cjk += 1
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def align_history_pairs(history: list[dict]) -> list[dict]:
    """把历史窗口对齐到"问答成对"：丢掉开头没有配对的 assistant。

    为什么需要：窗口是按条数截取的，起点可能落在"某轮的回答"上，
    于是模型看到的开场是"一个没有来由的答案"。这类孤立回答既占预算又容易误导，
    直接丢掉；末尾的孤立 user（提问尚无回答，如被中断的那轮）保留——它本来就是待办。
    """
    if not history:
        return []
    idx = 0
    while idx < len(history) and history[idx].get("role") != "user":
        idx += 1
    return history[idx:]


def compact_result_json(result_str: str, max_chars: int = _MAX_RESULT_CHARS) -> str:
    """把工具返回的 JSON 结果压缩成摘要，用于注入 LLM 上下文。

    - 输入不是 JSON 或较短时原样返回
    - error 结果完整保留（自纠错依赖错误与 hint）
    - **元数据类结果（无 rows 键，如 list_datasources 的全量列名）**：不套 1500 一刀切，
      改用更大的保底上限（RESULT_MAX_META_CHARS），保证 LLM 能拿到完整列名/描述
    - 查询类结果（含 rows）：rows 只留前 _MAX_ROWS 行；insights 等建议列表完整保留，
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

    # 元数据类（无 rows 数据行）→ 用较大的保底上限，避免列名/描述被腰斩
    has_rows = isinstance(obj.get("rows"), list)
    cap = max_chars if has_rows else max(
        max_chars,
        _chars_for_tokens(int(getattr(settings, "TOOL_RESULT_META_MAX_TOKENS", 3200) or 3200), _TOKENS_PER_CHAR_JSON),
    )

    out: dict[str, Any] = {}
    for k, v in obj.items():
        if k == "rows" and isinstance(v, list):
            out["rows"] = v[:_MAX_ROWS]
            out["rows_total"] = len(v)
            out["rows_truncated"] = len(v) > _MAX_ROWS
        else:
            out[k] = v  # insights 等建议列表完整保留，仅受 cap 兜底
    s = json.dumps(out, ensure_ascii=False, default=str)
    if len(s) > cap:
        s = s[:cap] + "…(已截断)"
    return s


def compress_history(messages: list[dict], keep: int = 0, max_chars: int = 0) -> list[dict]:
    """历史消息压缩：保留 system 首条 + 最近 keep 条；总长超限时把最早部分折叠为摘要行。

    返回：压缩后的消息列表（不修改入参）。
    """
    if not messages:
        return messages
    keep = keep or settings.CONTEXT_KEEP
    max_chars = max_chars or _chars_for_tokens(
        int(getattr(settings, "MESSAGE_LIST_MAX_TOKENS", 90000) or 90000), _TOKENS_PER_CHAR_MIXED
    )
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


# 跨轮记忆标记（lead_tools worker 注入沿用；对应已废弃的 smart_compress 语义）
COMPRESSION_MARKER = "【压缩摘要】"


# 跨轮记忆标记（lead_tools worker 注入沿用；对应已废弃的 smart_compress 语义——
# 生产代码无调用方，已于阶段 4 连同其测试/demo 一并删除）
COMPRESSION_MARKER = "【压缩摘要】"
