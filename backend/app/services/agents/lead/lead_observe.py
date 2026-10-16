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
    db_session,
    user_id: str,
    *,
    datasource_id=None,
    inject_fields_unselected: bool = False,
) -> list[dict]:
    """可用数据源清单（表级摘要；已选源带字段）。失败返回空列表。

    inject_fields_unselected：画布入口用——未选源时也给**首个数据源**注入真实字段，
    让执行器在规划/落块前就拿到列名（与 legacy 决策路径同一语义）。
    """
    from app.services.agents.lead.lead_tools import _load_available_datasources

    try:
        return await _load_available_datasources(
            db_session, user_id, datasource_id,
            inject_fields_unselected=inject_fields_unselected,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[lead_observe] list_datasources_failed", exc_info=True)
        return []


def list_metrics(ctx) -> str:
    """受治理指标清单（只读）。

    P3 直接读**已经注入上下文的那份**（ctx.metrics_ctx，来自指标语义层），
    不另开一条取数路径——同一事实只应有一个来源，否则两边迟早不一致。
    """
    return str(getattr(ctx, "metrics_ctx", "") or "")


async def recon_tool(
    name: str,
    *,
    ctx,
    args: dict | None = None,
    db_session=None,
    trace=None,
    degradation: list[str] | None = None,
):
    """执行一次只读观察（P3：独立 trace 名 lead_recon；失败即降级）。

    返回观察结果（str 或 list）；未接的工具返回 None（调用方据此记降级）。
    收敛在这里的意义：观察的 trace / 降级 / 工具名分派只有一份实现，
    循环里不再散落 if-elif 与 try/except。
    """
    args = args or {}
    span_cm = None
    if trace is not None:
        try:
            from app.services.observability import observe_tool_call

            span_cm = observe_tool_call(trace, "lead_recon:" + name, args=args)
            span_cm.__enter__()
        except Exception:  # noqa: BLE001
            span_cm = None
    try:
        if name == "get_canvas_layout":
            return await get_canvas_layout(ctx)
        if name == "list_datasources":
            return await list_datasources(
                db_session, str(ctx.user_id), datasource_id=ctx.datasource_id
            )
        if name == "list_fields":
            return await list_fields(db_session, str(ctx.user_id), args.get("datasource_id"))
        if name == "list_metrics":
            return list_metrics(ctx)
        # query_sample：需要真实取数（list_fields → 组列名 → LIMIT 5）。项目禁止 SELECT *，
        # 这条链路要连 SQL guard 一起验，**留到能用真机验证时再接**——
        # 现在返回 None，由调用方记 degraded，绝不假装成功。
        if degradation is not None:
            degradation.append("lead_agent_observe_unimplemented_" + name)
        return None
    except Exception:  # noqa: BLE001
        logger.warning("[lead_observe] recon_failed name=%s", name, exc_info=True)
        if degradation is not None:
            degradation.append("lead_agent_observe_failed_" + name)
        return None
    finally:
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


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
