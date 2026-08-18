"""上下文压缩工具：工具结果摘要化 + 历史消息压缩。

用途：控制注入 LLM 上下文的体积，降低 token 消耗、防止长结果撑爆上下文。

原则：
- error 结果不压缩（错误信息与 hint 必须完整回传给 LLM 用于自纠错）
- 成功结果保留结构（summary/columns/统计字段），只截断数据主体（rows/insights 等）
"""
from __future__ import annotations

import json
from typing import Any

_MAX_RESULT_CHARS = 1500  # 单个工具结果注入上下文的最大字符数
_MAX_ROWS = 10            # rows 保留前 N 行
_MAX_ITEMS = 5            # insights/suggestions/recommendations 保留前 N 条


def compact_result_json(result_str: str, max_chars: int = _MAX_RESULT_CHARS) -> str:
    """把工具返回的 JSON 结果压缩成摘要，用于注入 LLM 上下文。

    - 输入不是 JSON 或较短时原样返回
    - error 结果完整保留（自纠错依赖错误与 hint）
    - 成功结果：rows 只留前 _MAX_ROWS 行，insights 等只留前 _MAX_ITEMS 条
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
        elif k in ("insights", "suggestions", "recommendations") and isinstance(v, list):
            out[k] = v[:_MAX_ITEMS]
            out[f"{k}_total"] = len(v)
            out[f"{k}_truncated"] = len(v) > _MAX_ITEMS
        else:
            out[k] = v
    s = json.dumps(out, ensure_ascii=False, default=str)
    if len(s) > max_chars:
        s = s[:max_chars] + "…(已截断)"
    return s


def compress_history(messages: list[dict], keep: int = 20, max_chars: int = 8000) -> list[dict]:
    """历史消息压缩：保留 system 首条 + 最近 keep 条；总长超限时把最早部分折叠为摘要行。

    返回：压缩后的消息列表（不修改入参）。
    """
    if not messages:
        return messages
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