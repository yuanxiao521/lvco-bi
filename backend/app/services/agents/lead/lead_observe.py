# -*- coding: utf-8 -*-
"""观察能力（只读）：把「Lead 能自己去看什么」抽成**显式可调用函数**。

P0 只做「能力接口」，**不改任何行为**：本模块全是对既有实现的**薄委托**，
没有新逻辑，也还没有被主循环调用——P3 才把它们注册成 Lead 的只读工具
（每轮 ≤2 次、独立 trace 名 `lead_recon`）。

为什么必须先收敛：P3 要给 Lead「眼睛」，但"看一眼"的三件事现在散在三处——
  1. 画布现状   → `CanvasStateProvider.layout()`（挂在 `ctx.canvas_state`）
  2. 可用数据源 → `lead_tools._load_available_datasources()`（私有名）
  3. 字段清单   → 执行器侧的 `list_fields` 工具（agent_tools.py）
不先收敛成显式函数，P3 的工具层就得往三个模块里伸手，边界立刻糊掉。

设计约束（对齐任务书 §2.2 / §6）：
  · **只读**：本模块不产生任何写操作（写工具永不进 LLM 调用面）；
  · **失败即降级**：拿不到就返回空值，绝不抛给主循环（§6 不变量 4）；
  · **不做截断/压缩**：那是 Compressor 的事（§5.3）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("lvco.lead.observe")


async def get_canvas_layout(ctx) -> str:
    """画布现状（已落盘的块 / 图表 / 布局文本）。无画布上下文时返回空串。"""
    provider = getattr(ctx, "canvas_state", None)
    if provider is None:
        return ""
    try:
        # force=True：绕过 provider 的请求级缓存，读"此刻真实落盘"的布局
        # （与 lead_dispatch 落块后重读的语义一致）
        return await provider.layout(force=True) or ""
    except Exception:  # noqa: BLE001
        logger.warning("[lead_observe] get_canvas_layout_failed", exc_info=True)
        return ""


async def list_datasources(
    db_session, user_id: str, *, datasource_id=None
) -> list[dict]:
    """可用数据源清单（表级摘要；已选源带字段）。失败返回空列表。"""
    from app.services.agents.lead.lead_tools import _load_available_datasources

    try:
        return await _load_available_datasources(db_session, user_id, datasource_id)
    except Exception:  # noqa: BLE001
        logger.warning("[lead_observe] list_datasources_failed", exc_info=True)
        return []


async def list_fields(db_session, user_id: str, datasource_id) -> list[dict]:
    """指定数据源的字段清单（列名 + 类型）。

    委托给数据源加载逻辑（已选源会带上 fields），**不另写一份取字段的代码**——
    两份实现必然跑偏（本项目已在事件表上踩过一次同样的坑）。
    """
    ds = await list_datasources(db_session, user_id, datasource_id=datasource_id)
    if not ds:
        return []
    fields = ds[0].get("fields")
    return list(fields) if isinstance(fields, list) else []
