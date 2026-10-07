# -*- coding: utf-8 -*-
"""画布状态感知（请求级）：读一次、缓存、统一出口。

**为什么单独抽一层**（此前是"能力层定义 + 决策/执行层逐点注入"）：

1. 同一件事原先有 4 处独立注入、两种形态（`canvas_layout` 参数 / 手工拼进 `history` 的
   assistant 消息），于是**每新增一个分支就必须记得手动注入一次**——`_answer_branch`
   漏掉画布快照就是这个写法的必然产物，而不是偶发 bug；
2. 同一轮里 `_load_canvas_snapshot` 最多被读 3 次（决策 + Worker + 执行摘要），无缓存；
3. 两种注入形态格式不统一，后来人不知道该按哪个来。

本模块把"读画布 → 渲染成可注入文本"收敛成**一个入口 + 请求级缓存 + 两个出口**：
- `layout(force=False)`：给 LLM 看的布局文本（带 [A1]/[B1] 稳定编号，≤900 字）；
- `narrative()`：结论要点（从文本块里提取，≤96 字，用于收尾话术与记忆）；
- `state()`：结构化快照（供规则评审/摘要/将来的无头用法）。

缓存语义：默认缓存，**落块后需显式 `force=True` 重读**（落块是前端写库，只有重读才看得见）。

注意：`Canvas.blocks` 的唯一写入者是前端（`PUT /canvases/{id}/blocks`）。后端只发
`canvas_action` 事件、不写库，因此无头用法（纯 API 客户端）下这里恒为空——这是已知的
架构约束，集中在本模块说明，便于将来在这里加"服务端直接落库"的开关。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("lvco.lead.canvas_state")


@dataclass
class CanvasState:
    """一次请求内的画布状态快照。"""

    canvas_id: str
    block_count: int = 0
    layout_text: str = ""                     # render_canvas_layout 的输出（已限长）
    narrative: str = ""                       # 结论要点（可选，需显式加载）
    raw_blocks: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "canvas_id": self.canvas_id,
            "block_count": self.block_count,
            "layout": self.layout_text,
            "narrative": self.narrative,
        }


class CanvasStateProvider:
    """请求级画布状态提供者：绑一次 db/user/canvas，内部缓存，统一出口。

    用法（在 `LeadAgent.stream()` 开头构造一次，全请求复用）：
        ctx.canvas_state = CanvasStateProvider(db_session, user_id, canvas_id, entry)
    消费点统一写 `layout = await ctx.canvas_state.layout()`。
    """

    def __init__(self, db_session, user_id: str | int, canvas_id: str | None,
                 entry: str = "chat") -> None:
        self._db = db_session
        self._user_id = str(user_id or "")
        self._canvas_id = str(canvas_id) if canvas_id else ""
        self._entry = entry
        self._state: CanvasState | None = None
        self._narrative_loaded = False

    # ── 可用性 ────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """只有画布入口且带 canvas_id 时才谈得上"画布状态"。"""
        return bool(self._entry == "canvas" and self._canvas_id and self._user_id and self._db)

    # ── 出口 1：布局文本（给 LLM）────────────────────────────
    async def layout(self, force: bool = False) -> str:
        """画布布局文本；不可用时返回空串（调用方按"无画布状态"处理）。

        force=True：跳过缓存重读一次（落块后必须用，否则看不到新块）。
        """
        if not self.enabled:
            return ""
        if self._state is None or force:
            await self._load()
        return self._state.layout_text if self._state else ""

    # ── 出口 2：结论要点 ─────────────────────────────────────
    async def narrative(self) -> str:
        """从已落盘的文本块里提取结论要点（≤96 字）。只在需要时读一次。"""
        if not self.enabled:
            return ""
        if self._state is None:
            await self._load()
        if self._narrative_loaded:
            return self._state.narrative if self._state else ""
        self._narrative_loaded = True
        try:
            from app.services.agents.lead.lead_tools import _load_canvas_narrative

            text = await _load_canvas_narrative(self._db, self._user_id, self._canvas_id)
            if self._state is not None and text:
                self._state.narrative = text
        except Exception as e:  # noqa: BLE001
            logger.warning("[canvas_state] narrative_failed: %s", e)
        return self._state.narrative if self._state else ""

    # ── 出口 3：结构化快照 ───────────────────────────────────
    async def state(self, force: bool = False) -> CanvasState | None:
        if not self.enabled:
            return None
        if self._state is None or force:
            await self._load()
        return self._state

    # ── 内部：真正的读取（一处读、一处渲染）──────────────────
    async def _load(self) -> None:
        from app.services.agents.lead.lead_tools import _load_canvas_blocks

        blocks: list | None = None
        try:
            blocks = await _load_canvas_blocks(self._db, self._user_id, self._canvas_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[canvas_state] load_failed: %s", e)
        if blocks is None:
            # 画布不存在 / 读取失败 → 与旧行为一致：不产出任何布局文本
            self._state = CanvasState(canvas_id=self._canvas_id)
            return
        from app.services.canvas_tools import render_canvas_layout

        self._state = CanvasState(
            canvas_id=self._canvas_id,
            block_count=len(blocks),
            raw_blocks=list(blocks),
            layout_text=render_canvas_layout(blocks, canvas_id=self._canvas_id),
        )
        if self._narrative_loaded:
            self._narrative_loaded = False   # 重读布局后要点需重新提取
