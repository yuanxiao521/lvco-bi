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

from app.services.profile_schema import null_rate as _null_rate, quote_ident, type_kind

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

    P4-B：观察调用绕过 ToolExecutor，所以这里**自己写一条 agent_calls tool 行**，
    并套 parent="recon" 作用域——否则排查时永远看不到"Lead 到底看了什么才这么答"。
    """
    import time

    from app.services import agent_trace

    args = args or {}
    started = time.perf_counter()
    value = None
    span_cm = None
    if trace is not None:
        try:
            from app.services.observability import observe_tool_call

            span_cm = observe_tool_call(trace, "lead_recon:" + name, args=args)
            span_cm.__enter__()
        except Exception:  # noqa: BLE001
            span_cm = None
    # P4-B：进入 recon 作用域（内部记录自然带 parent=recon；退出即还原，不泄漏）
    with agent_trace.parent_scope("recon"):
        try:
            value = await _recon_dispatch(
                name=name, ctx=ctx, args=args,
                db_session=db_session, degradation=degradation,
            )
            return value
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
            try:
                preview = (
                    value if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False, default=str) if value is not None else ""
                )
                agent_trace.record({
                    "kind": "tool",
                    "tool_name": "recon:" + name,
                    "args": args,
                    "ok": value is not None,
                    "error_kind": None if value is not None else "tool_error",
                    "result_preview": preview[:500],
                    "result_chars": len(preview),
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "parent": "recon",
                })
            except Exception:  # noqa: BLE001
                logger.debug("[lead_observe] recon record failed", exc_info=True)


async def _recon_dispatch(
    *,
    name: str,
    ctx,
    args: dict,
    db_session,
    degradation: list[str] | None,
):
    """观察工具的实际分派（纯查询，不含记录/作用域；见 recon_tool 的说明）。"""
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
    if name == "profile_datasource":
        # 全表画像：与 query_sample 同一条执行链，但产出的是"能分析什么"而不是"长什么样"
        return await profile_datasource(
            db_session,
            str(getattr(ctx, "user_id", "") or ""),
            args.get("datasource_id") or getattr(ctx, "datasource_id", None),
            degradation=degradation,
        )
    # 未知/未接的观察工具：记降级，绝不假装成功
    if degradation is not None:
        degradation.append("lead_agent_observe_unimplemented_" + name)
    return None


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

# ── profile_datasource：全表数据画像（P1-D）─────────────────────────────────
# 为什么不是"再取几行样本"：样本只能说明"长什么样"，说明不了"能分析什么"——
#   · 有没有可用的时间列（趋势分析的前提）；
#   · 维度基数（distinct=3 适合饼图，distinct=20000 落块就是灾难）；
#   · 空值率（空值率高说明该列不可作为主维度/口径来源）；
#   · 数值范围（量纲异常、负值、0 占比高会改变图表选择）。
# 这些都必须**全表聚合**才能得到。一次扫描同时算出所有列的统计量，
# 成本与一次 COUNT 同阶，比让模型反复 query_sample 便宜得多。
_PROFILE_MAX_COLUMNS = 20        # 宽表只统计前 N 列：画像是"看形态"，不是导出
_PROFILE_CELL_CHARS = 40         # 单个 min/max 值截断（长文本列可能很长）

# 类型判定/空值率/标识符转义统一来自 app.services.profile_schema（唯一真源）：
# REST 侧画像（services/datasource_profile.py）与这里的口径必须一致，否则同一张表
# 会给出两套"空值率/基数"。共用后本模块只保留"呈现形态"差异。


def profile_sql(table_ref: str, fields: list[dict]) -> str:
    """构造全表聚合 SQL（纯函数，可单测）。

    一次扫描产出：总行数 / 每列 distinct / 每列空值数 / 数值与时间列的 min-max
    （数值列额外算 avg）。**别名用位置下标 c0/c1…**，不直接拿列名当别名——
    真实业务列名可能带空格/引号/标点，塞进 SQL 别名会被解析器打断。

    只对**结构化类型**做 MIN/MAX/AVG：
      · other 列（TEXT/BLOB/STRUCT）不做 MIN/MAX——比较无意义，且可能抛错；
      · avg 用 TRY_CAST 兜底，脏列不会把整个聚合语句打挂（一条语句算全部列，
        任何一列报错就等于整张画像失败，所以必须防御）。
    """
    cols = [f for f in (fields or []) if isinstance(f, dict) and f.get("name")]
    cols = cols[:_PROFILE_MAX_COLUMNS]
    if not cols or not table_ref:
        return ""
    items: list[str] = ['COUNT(*) AS "total_rows"']
    for i, f in enumerate(cols):
        kind = type_kind(f.get("data_type"))
        quoted = quote_ident(f["name"])
        items.append(f'COUNT(DISTINCT {quoted}) AS "c{i}__nd"')
        items.append(
            f'SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END) AS "c{i}__nulls"'
        )
        if kind in ("numeric", "temporal"):
            items.append(f'MIN({quoted}) AS "c{i}__min"')
            items.append(f'MAX({quoted}) AS "c{i}__max"')
        if kind == "numeric":
            items.append(f'AVG(TRY_CAST({quoted} AS DOUBLE)) AS "c{i}__avg"')
    return f"SELECT {', '.join(items)} FROM {table_ref}"


def _cell(value) -> object:
    """min/max 值归一：标量原样，其余转字符串并截断。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    return text[:_PROFILE_CELL_CHARS] + "…" if len(text) > _PROFILE_CELL_CHARS else text


def _num(value):
    """聚合值转数字（None / 非数字 → None）；avg 保留 4 位。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 4)


def build_profile_payload(
    *,
    datasource_id: object,
    datasource_name: str,
    table_ref: str,
    fields: list[dict],
    columns: list,
    row: list,
) -> dict:
    """把一次聚合查询的结果组装成画像 payload（纯函数，可单测）。

    输出刻意保持**紧凑**：列数多时只给前三高基数列的提示，避免画像本身把上下文吃满。
    """
    cols = [f for f in (fields or []) if isinstance(f, dict) and f.get("name")][
        :_PROFILE_MAX_COLUMNS
    ]
    values = dict(zip([str(c) for c in (columns or [])], list(row or [])))
    total = int(values.get("total_rows") or 0)

    out_cols: list[dict] = []
    highlights: list[str] = []
    for i, f in enumerate(cols):
        name = str(f["name"])
        kind = type_kind(f.get("data_type"))
        nulls = _num(values.get(f"c{i}__nulls")) or 0
        item = {
            "name": name,
            "data_type": str(f.get("data_type") or "?"),
            "category": str(f.get("category") or ""),
            "distinct": int(_num(values.get(f"c{i}__nd")) or 0),
            "null_rate": _null_rate(total, total - nulls, digits=4),
        }
        if kind in ("numeric", "temporal"):
            item["min"] = _cell(values.get(f"c{i}__min"))
            item["max"] = _cell(values.get(f"c{i}__max"))
        if kind == "numeric":
            item["avg"] = _num(values.get(f"c{i}__avg"))
        out_cols.append(item)
        if kind == "temporal" and item.get("min") is not None:
            highlights.append(f"{name} 时间范围 {item['min']} ~ {item['max']}")
        elif kind == "numeric" and item.get("min") is not None:
            avg_txt = f"，均值 {item['avg']}" if item.get("avg") is not None else ""
            highlights.append(f"{name} 数值范围 {item['min']} ~ {item['max']}{avg_txt}")

    low_card = sorted(
        (c for c in out_cols if c["distinct"] and c["distinct"] <= 50),
        key=lambda c: c["distinct"],
    )[:3]
    high_null = sorted(
        (c for c in out_cols if (c["null_rate"] or 0) > 0),
        key=lambda c: c["null_rate"],
        reverse=True,
    )[:3]
    if low_card:
        highlights.append(
            "低基数列（适合做维度）："
            + "、".join(f"{c['name']}({c['distinct']})" for c in low_card)
        )
    if high_null:
        highlights.append(
            "空值列（慎作口径来源）："
            + "、".join(f"{c['name']}({c['null_rate']:.0%})" for c in high_null)
        )

    return {
        "ok": True,
        "datasource_id": str(datasource_id),
        "datasource_name": datasource_name,
        "table_ref": table_ref,
        "row_count": total,
        "column_count": len(fields or []),
        "analyzed_columns": len(out_cols),
        "truncated_columns": len(fields or []) > len(out_cols),
        "columns": out_cols,
        "highlights": highlights[:6],
        "note": "以上为全表聚合统计（一次扫描），不是样本行；字段名必须原样使用，勿汉化。",
    }


async def profile_datasource(
    db_session,
    user_id: str,
    datasource_id=None,
    *,
    degradation: list[str] | None = None,
) -> str | None:
    """全表数据画像（行数 / 每列 distinct / 空值率 / 数值 min-max / 时间列范围）。

    与 query_sample 同一套硬约束（见本模块上文）：
      1. 不另写执行器——走 QueryDatasourceTool().execute()，L3 guard、表归属校验、
         超时保护、只读 DuckDB 全在那条链路上；
      2. 禁 SELECT *（聚合语句按列显式构造）；
      3. 失败即降级（返回可读错误 JSON / None），绝不返回伪造统计。
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
        # 不退回别的源：用错数据算出来的画像是"看起来对但完全错"的最坏情况
        _degrade("profile_datasource_not_found")
        return None

    fields = [f for f in (ds.get("fields") or []) if isinstance(f, dict) and f.get("name")]
    table_ref = str(ds.get("table_ref") or "")
    if not fields:
        _degrade("profile_datasource_no_fields")
        return None
    if not table_ref:
        _degrade("profile_datasource_no_table_ref")
        return None

    sql = profile_sql(table_ref, fields)
    if not sql:
        _degrade("profile_datasource_empty_sql")
        return None

    from app.services.agent_tools import QueryDatasourceTool

    try:
        raw = await QueryDatasourceTool().execute(
            datasource_id=str(ds.get("id")),
            sql=sql,
            user_id=str(user_id),
            db_session=db_session,
        )
    except Exception:  # noqa: BLE001
        logger.warning("[lead_observe] profile_datasource_failed", exc_info=True)
        _degrade("profile_datasource_failed")
        return None

    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except Exception:  # noqa: BLE001
        _degrade("profile_datasource_bad_payload")
        return None

    if payload.get("error"):
        _degrade("profile_datasource_query_error")
        # 可读错误 + hint + sql：让模型能自纠（换源 / 先 list_fields），而不是重试同一个错
        return json.dumps({
            "ok": False,
            "error": str(payload.get("error"))[:300],
            "table_ref": payload.get("table_ref") or table_ref,
            "hint": payload.get("hint") or "先 list_fields 确认字段，或换一个数据源再画像",
            "sql": sql,
        }, ensure_ascii=False)

    columns = [str(c) for c in (payload.get("columns") or [])]
    rows = payload.get("rows") or []
    if not rows:
        _degrade("profile_datasource_empty_result")
        return json.dumps({
            "ok": False,
            "error": "聚合查询未返回结果（表为空或统计列全部不可用）",
            "table_ref": table_ref,
            "sql": sql,
        }, ensure_ascii=False)

    built = build_profile_payload(
        datasource_id=ds.get("id"),
        datasource_name=str(ds.get("name") or ""),
        table_ref=table_ref,
        fields=fields,
        columns=columns,
        row=rows[0],
    )
    if not built.get("row_count"):
        _degrade("profile_datasource_empty_table")
        return json.dumps({
            "ok": False,
            "error": "表为 0 行（可能是同步失败或过滤条件导致），画像无意义",
            "table_ref": table_ref,
        }, ensure_ascii=False)
    return json.dumps(built, ensure_ascii=False, default=str)

