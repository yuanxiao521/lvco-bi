"""L2 冷层：长期记忆（ai_memories 摘要）。"""

from __future__ import annotations

from app.services.context.types import Block, BlockKind, Priority


class MemorySource:
    """长期记忆源：一段已生成的结构化摘要（四节：口径/数字/数据源/未决）。"""

    def __init__(self, summary: str) -> None:
        self._summary = (summary or "").strip()

    def load(self, *, force: bool = False) -> Block:
        return Block(
            kind=BlockKind.MEMORY,
            text=self._summary,
            priority=Priority.MEMORY,
            compressible=True,
            min_tokens=0,
        )
