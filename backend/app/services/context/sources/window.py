"""L1 温层：最近对话原文。

铁律：Source 只取不裁——这里只负责"选哪些条"，渲染与截断交给装配层。
P0：选点算法从 LeadContext.recent_turns 原样迁移（逐字节等价，由既有测试锁住）；
窗口所有权收归服务端（DB 取数）是阶段 3 的事。
"""

from __future__ import annotations

from app.config import settings
from app.services.context.types import Block, BlockKind, Priority


def select_for_answer(
    turns: list[dict],
    max_turns: int = 12,
    max_chars: int | None = None,
) -> list[dict]:
    """回答分支的窗口选点（原 LeadContext.recent_turns 的算法，逐字节等价）。

    规则：取最近若干"整条"消息；总字符超预算即停；只有最新的那一条自身超预算时
    才截断它；最后丢掉开头的孤立回答——除非整个窗口只剩回答（保住一点线索）。
    """
    limit = int(max_chars or getattr(settings, "LEAD_CTX_ANSWER_HISTORY_TOKENS", 2000) or 2000)
    picked: list[dict] = []
    used = 0
    for t in reversed(turns[-max(1, max_turns):]):
        role = t.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(t.get("content", ""))
        if used + len(content) > limit:
            if not picked and limit - used > 40:
                picked.append({
                    "role": role,
                    "content": content[: limit - used - 1] + "…",
                })
            break
        picked.append({"role": role, "content": content})
        used += len(content)
    picked.reverse()
    if any(t["role"] == "user" for t in picked):
        while picked and picked[0]["role"] != "user":
            picked.pop(0)
    return picked


class WindowSource:
    """近端窗口源。

    Args:
        turns: 会话活记忆（user/assistant 交替的 dict 列表）。
        max_turns: 窗口上限（P0 沿用旧配置的"条"语义，阶段 3 改为"轮"）。
    """

    def __init__(self, turns: list[dict], max_turns: int) -> None:
        self._turns = turns
        self._max_turns = max(1, int(max_turns))

    def load(self, *, force: bool = False) -> Block:
        """取最近 N 条消息（结构化，不渲染——渲染是装配层的事）。"""
        window = list(self._turns[-self._max_turns:])
        return Block(
            kind=BlockKind.WINDOW,
            messages=tuple(window),
            priority=Priority.WINDOW,
            compressible=False,   # L1 丢不起：近端原文不压缩（P0 语义）
            min_tokens=0,
        )
