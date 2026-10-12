# -*- coding: utf-8 -*-
"""画布动作落库器（后端唯一落库）。

CANVAS_BACKEND_PERSIST=true 时，canvas_tools 的每个写动作在返回 canvas_action
前先持久化到 DB（canvases.blocks，经 CanvasRepository）——AI 落块不再依赖前端消费，
断线/后台任务场景下块不丢，assess_subtask 读 DB 即真相源。

与前端 handleCanvasAction 行为对齐（同一动作产生相同的块）：
- add_chart_block：校验（measures 非空；kpi_card 允许零维度）→ next_chart_slot 定位
  → 追加 chart 块 + 嵌入 _chartConfig/_chartResult（rows/columns 后端已取）
- add_text_block：next_full_width_slot 定位 → 追加通栏块
- update_chart_block：按 blockId 更新 title/chartType + _chartConfig
  （rows 由前端字段变化后重新查询、经既有 PUT 更新——与前端现状一致）
- remove_block：按 blockId 删除；clear_canvas：清空
- arrange_layout：canvas_layout_service.apply_report_layout 全量重排

布局宽度用 REPORT.fallbackW（前端 resolveGeometry 的无调用方回退值）；
前端按块自身 x/y/width 绝对定位渲染，容器更宽只是留白，不影响正确性。
"""
from __future__ import annotations

import logging
import random
import time
import uuid as uuid_mod

from app.repositories.canvas_repository import SQLAlchemyCanvasRepository
from app.services import canvas_layout_service as laysvc

log = logging.getLogger("lvco.canvas_block_writer")


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        out = digits[n % 36] + out
        n //= 36
    return out


def _new_block_id(prefix: str) -> str:
    """与前端 newBlockId 同格式：{prefix}_{ms基36}{5位随机基36}。"""
    ms = int(time.time() * 1000)
    rand = "".join(random.choice("0123456789abcdefghijklmnopqrstuvwxyz") for _ in range(5))
    return f"{prefix}_{_b36(ms)}{rand}"


def _uuid(value: str):
    try:
        return uuid_mod.UUID(str(value))
    except (TypeError, ValueError):
        return None


class CanvasBlockWriter:
    """按 canvas_action 变更 DB blocks；成功返回增强后的 action，失败/不支持返回 None。"""

    def __init__(self, db, user_id: str, canvas_id: str) -> None:
        self.repo = SQLAlchemyCanvasRepository(db)
        self.user_id = user_id
        self.canvas_id = canvas_id
        self._ctx: dict = {}

    async def apply(self, action: dict) -> dict | None:
        self._ctx = action
        kind = action.get("action")
        cid, uid = _uuid(self.canvas_id), _uuid(self.user_id)
        if cid is None or uid is None:
            return None
        canvas = await self.repo.get_by_id(cid, uid)
        if canvas is None:
            log.warning("[block_writer] canvas_not_found canvas_id=%s", self.canvas_id)
            return None
        blocks: list[dict] = list(canvas.blocks or [])

        handler = {
            "add_chart_block": self._add_chart,
            "add_text_block": self._add_text,
            "update_chart_block": self._update_chart,
            "remove_block": self._remove,
            "clear_canvas": self._clear,
            "arrange_layout": self._arrange,
        }.get(kind)
        if handler is None:
            return None

        result = await handler(blocks)
        if result is None:
            return None
        new_blocks, extra = result
        await self.repo.update_blocks(cid, uid, new_blocks)
        action = {**action, **(extra or {}), "persisted": True}
        log.info("[block_writer] persisted kind=%s canvas_id=%s blocks=%d",
                 kind, self.canvas_id, len(new_blocks))
        return action

    # ---------- 各动作 ----------

    async def _add_chart(self, blocks: list[dict]) -> tuple[list[dict], dict] | None:
        action_block = dict((self._ctx or {}).get("block") or {})
        title = action_block.get("title") or "图表"
        ct = action_block.get("chartType") or "bar"
        qc = dict(action_block.get("queryConfig") or {})
        dims = qc.get("dimensions") or []
        meas = qc.get("measures") or []
        # 与前端 add 分支同校验
        if not meas:
            return None
        if not dims and ct != "kpi_card":
            return None

        geo = laysvc.resolve_geometry()
        slot = laysvc.next_chart_slot(blocks)
        block_id = _new_block_id("chart")
        new_block = {
            "type": "chart",
            "blockId": block_id,
            "title": title,
            "chartType": ct,
            "renderer": "echarts",
            "datasourceId": action_block.get("datasourceId"),
            "width": geo["chartW"],
            "height": laysvc.REPORT["chartH"],
            "x": slot["x"],
            "y": slot["y"],
            "_chartConfig": {
                "dimensions": dims,
                "measures": meas,
                "filters": [],
                "chartType": ct,
                "limit": qc.get("limit") or 20,
                "datasourceId": action_block.get("datasourceId"),
            },
        }
        rows = action_block.get("rows")
        if isinstance(rows, list):
            new_block["_chartResult"] = {
                "columns": action_block.get("columns") or [],
                "rows": rows,
                "chartType": ct,
                "queryTimeMs": 0,
            }
        new_blocks = blocks + [new_block]
        return new_blocks, {"block": {**action_block, "blockId": block_id,
                                      "x": slot["x"], "y": slot["y"],
                                      "width": geo["chartW"], "height": laysvc.REPORT["chartH"]},
                            "blockId": block_id}

    async def _add_text(self, blocks: list[dict]) -> tuple[list[dict], dict] | None:
        action_block = dict((self._ctx or {}).get("block") or {})
        block_type = action_block.get("blockType") or "text"
        content = action_block.get("content") or ""
        geo = laysvc.resolve_geometry()
        slot = laysvc.next_full_width_slot(blocks)
        block_id = _new_block_id(block_type)
        new_block = {
            "type": block_type,
            "blockId": block_id,
            "content": content,
            "width": geo["fullW"],
            "x": slot["x"],
            "y": slot["y"],
        }
        return blocks + [new_block], {"block": {**action_block, "blockId": block_id,
                                                "x": slot["x"], "y": slot["y"],
                                                "width": geo["fullW"]},
                                      "blockId": block_id}

    async def _update_chart(self, blocks: list[dict]) -> tuple[list[dict], dict] | None:
        ctx = self._ctx or {}
        block_id = ctx.get("blockId")
        patch = dict(ctx.get("patch") or {})
        if not block_id:
            return None
        changed = False
        new_blocks = []
        for b in blocks:
            if b.get("type") == "chart" and b.get("blockId") == block_id:
                nb = dict(b)
                cfg = dict(b.get("_chartConfig") or {})
                if "title" in patch:
                    nb["title"] = patch["title"]
                if "chartType" in patch:
                    nb["chartType"] = patch["chartType"]
                    cfg["chartType"] = patch["chartType"]
                if "dimensions" in patch:
                    cfg["dimensions"] = patch["dimensions"]
                if "measures" in patch:
                    cfg["measures"] = patch["measures"]
                nb["_chartConfig"] = cfg
                new_blocks.append(nb)
                changed = True
            else:
                new_blocks.append(b)
        return (new_blocks, {}) if changed else None

    async def _remove(self, blocks: list[dict]) -> tuple[list[dict], dict] | None:
        block_id = (self._ctx or {}).get("blockId")
        if not block_id:
            return None
        new_blocks = [b for b in blocks
                      if not (b.get("type") == "chart" and b.get("blockId") == block_id)]
        return new_blocks, {}

    async def _clear(self, blocks: list[dict]) -> tuple[list[dict], dict] | None:
        return [], {}

    async def _arrange(self, blocks: list[dict]) -> tuple[list[dict], dict] | None:
        return laysvc.apply_report_layout(blocks), {}

    # ctx 由 apply 的调用方临时注入（工具在调用前设置）——见 canvas_tools._maybe_persist


async def maybe_persist(action: dict, user_id: str, canvas_id: str, db) -> dict | None:
    """灰度入口：CANVAS_BACKEND_PERSIST 开启时把 action 持久化到 DB。

    返回增强后的 action（含 blockId/x/y/persisted 等）；开关关闭、画布缺失、
    动作不支持或校验未过时返回 None（调用方按原样返回 action）。
    """
    from app.config import settings
    if not getattr(settings, "CANVAS_BACKEND_PERSIST", False):
        return None
    writer = CanvasBlockWriter(db, user_id, canvas_id)
    writer._ctx = dict(action)  # 动作载荷作为各 handler 的 ctx
    return await writer.apply(action)
