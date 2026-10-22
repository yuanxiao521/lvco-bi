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

import json
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


# ── query_sample：真实取 5 行样本（P3 观察能力的最后一块）────────────────────
# 为什么必须真取数：模型幻觉最典型的两种形态就是「字段名猜错」和「值域猜错」
# （把 2023-01-15 00:00:00 当成 2023-01-15、把 "华东" 写成 "East"）。看一眼真实行，
# 比任何自然语言描述都有效，也比重派一次完整编排便宜得多。
# 三条硬约束（对齐任务书 §6 不变量 1/4）：
#   1. **禁 SELECT ***：列名从 schema 显式枚举并加双引号（guard 的 AST 层也会拦 *）；
#   2. **不另写执行器**：一律走 QueryDatasourceTool().execute() —— L3 guard、
#      表归属校验、超时保护、只读 DuckDB 全在那一条链路上，抄一份必然跑偏；
#   3. **失败即降级**：取不到就返回 None 并记 degraded，绝不返回伪造样本。
_SAMPLE_ROWS = 5
_SAMPLE_MAX_FIELDS = 12      # 宽表只取前 12 列：样本用于认形态，不是导出
_SAMPLE_CELL_CHARS = 80      # 单值截断，防止一个长文本列把上下文吃掉


def sample_sql(table_ref: str, fields: list[str], limit: int = _SAMPLE_ROWS) -> str:
    """构造样本 SQL（纯函数，可单测）：显式列名 + LIMIT，结构上不可能出现 SELECT *。"""
    cols = ", ".join('"' + str(f).replace('"', '""') + '"' for f in fields)
    return f"SELECT {cols} FROM {table_ref} LIMIT {int(limit)}"


def _sample_cell(value):
    """单元格归一：标量原样，其余转字符串并截断（样本只需看形态）。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:_SAMPLE_CELL_CHARS] + "…" if len(text) > _SAMPLE_CELL_CHARS else text


async def query_sample(
    db_session,
    user_id: str,
    datasource_id=None,
    *,
    degradation: list[str] | None = None,
) -> str | None:
    """取指定数据源的 5 行真实样本，返回紧凑 JSON 字符串。

    取不到时返回 None（调用方据此记降级），所有降级原因写进 degradation 列表。
    只提样本、不提全量：样本替代不了业务口径，它的作用是让 Lead 少猜字段和值域。
    """
    def _degrade(reason: str) -> None:
        if degradation is not None:
            degradation.append("lead_agent_observe_" + reason)

    target = str(datasource_id) if datasource_id else None
    candidates = await list_datasources(
        db_session, str(user_id), datasource_id=target, inject_fields_unselected=True,
    )
    ds = candidates[0] if candidates else None
    if ds is None:
        # 模型点名了一个不存在/无权限的源：不退回别的源（那会用错数据得出错误结论），
        # 直接降级，让它改用 list_datasources 重新确认。
        _degrade("query_sample_datasource_not_found")
        return None

    fields = [
        str(f.get("name") if isinstance(f, dict) else f)
        for f in (ds.get("fields") or [])
    ]
    fields = [f for f in fields if f][:_SAMPLE_MAX_FIELDS]
    if not fields:
        _degrade("query_sample_no_fields")
        return None
    table_ref = str(ds.get("table_ref") or "")
    if not table_ref:
        _degrade("query_sample_no_table_ref")
        return None

    sql = sample_sql(table_ref, fields)
    from app.services.agent_tools import QueryDatasourceTool

    try:
        raw = await QueryDatasourceTool().execute(
            datasource_id=str(ds.get("id")),
            sql=sql,
            user_id=str(user_id),
            db_session=db_session,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[lead_observe] query_sample_failed", exc_info=True)
        _degrade("query_sample_failed")
        return None

    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:  # noqa: BLE001
        _degrade("query_sample_bad_payload")
        return None

    if payload.get("error"):
        # 返回可读错误而不是 None：模型看到「为什么取不到」才会自纠（换列名 / 先
        # list_fields / 直接派发），否则它只会把同一个错误重试一遍。
        _degrade("query_sample_query_error")
        return json.dumps({
            "ok": False,
            "error": str(payload.get("error"))[:300],
            "table_ref": payload.get("table_ref") or table_ref,
            "hint": payload.get("hint") or "先 list_fields 确认字段名，再决定是否派发",
            "sql": sql,
        }, ensure_ascii=False)

    columns = [str(c) for c in (payload.get("columns") or fields)]
    rows: list[dict] = []
    for raw_row in (payload.get("rows") or [])[:_SAMPLE_ROWS]:
        if isinstance(raw_row, dict):
            items = [(c, raw_row.get(c)) for c in columns] or list(raw_row.items())
        elif isinstance(raw_row, (list, tuple)):
            items = list(zip(columns, raw_row))
        else:
            items = [("value", raw_row)]
        rows.append({str(k): _sample_cell(v) for k, v in items})

    if not rows:
        _degrade("query_sample_empty")
        return json.dumps({
            "ok": False,
            "error": "查询返回 0 行（表为空或过滤过严）",
            "table_ref": table_ref,
            "sql": sql,
        }, ensure_ascii=False)

    return json.dumps({
        "ok": True,
        "datasource_id": str(ds.get("id")),
        "table_ref": table_ref,
        "columns": columns[:_SAMPLE_MAX_FIELDS],
        "row_count": len(rows),
        "rows": rows,
        "note": f"仅 {len(rows)} 行样本，用于确认字段与值的真实形态（不是全量数据）",
    }, ensure_ascii=False, default=str)


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
        if name == "query_sample":
            # 真实取 5 行样本：内部复用 query_sql 的执行链（guard + 归属校验 + 超时），
            # 不另起一条取数实现。取不到时返回 None 并记降级。
            return await query_sample(
                db_session,
                str(getattr(ctx, "user_id", "") or ""),
                args.get("datasource_id") or getattr(ctx, "datasource_id", None),
                degradation=degradation,
            )
        # 未知/未接的观察工具：记降级，绝不假装成功
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
