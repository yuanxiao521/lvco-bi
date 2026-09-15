"""语义评测器 v2：结构化语义期待 vs 三种查询路径归一。

为什么重做：v1 判分把「期望 SQL 模板」执行结果与 Agent 结果集比较，
问题——①期望是 SQL 写法而非语义；②Lead 全链路走 query_engine 结构化/指标
引用，判分却只认裸 SQL；③时间基准漂移、图型近邻误伤，判得过于严厉。

v2 思路（本文件）：
1. 期望从 SQL 模板升级为「结构化语义期待」SemanticExpect（最终指标/维度/
   粒度/过滤/时间口径/Top-N/图表/回答要点）。
2. 三种查询路径统一归一为 SemanticQuery：
   - query_engine 结构化参数  → 直接映射
   - metric_key 指标引用      → 保留 key，可展开为 field+agg 指纹
   - query_sql 裸 SQL         → SQLGlot 解析聚合/分组/过滤/Limit/排序
3. 判分采用「期望 ⊆ Agent」的覆盖语义，多维度子项输出（不是一刀切 bool），
   解析不出的子项标 not_applicable 不计分（避免误伤）。

用法：semantic_run_eval.py 调用 judge_semantic(question, attempt)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 模板指标：key → (agg, 字段)。与 metric_service.DEFAULT_TEMPLATE_METRICS 一致
TEMPLATE_METRICS: dict[str, tuple[str, str]] = {
    "sales_amount": ("SUM", "amount"),
    "order_count": ("COUNT", "order_id"),
    "customer_count": ("COUNT_DISTINCT", "customer_id"),
    "avg_price": ("AVG", "amount"),
}

# 指标「名称」→ 指标 key（评审时把 LLM 用业务名当字段写成 field 的引用归一回到口径）
_METRIC_KEY_BY_NAME: dict[str, str] = {
    "销售额": "sales_amount", "销售金额": "sales_amount", "成交金额": "sales_amount",
    "订单量": "order_count", "订单数": "order_count", "订单笔数": "order_count",
    "客户数": "customer_count", "客户数量": "customer_count",
    "客单价": "avg_price", "平均客单价": "avg_price",
}
# field 位置出现指标 key 时，直接命中指标口径（LLM 习惯把 key 当字段传）
_FIELD_IS_METRIC = {k for k in TEMPLATE_METRICS}
_FIELD_ALIAS_TO_KEY = {**{k: k for k in TEMPLATE_METRICS},
                       **_METRIC_KEY_BY_NAME,
                       **{k.casefold(): k for k in TEMPLATE_METRICS}}

# 图型近邻：视觉/语义可接受的变体（缓解"过于严厉"）
CHART_EQUIV: dict[str, set[str]] = {
    "donut": {"pie"},
    "pie": {"donut"},
    "horizontal_bar": {"bar", "grouped_bar", "stacked_bar"},
    "grouped_bar": {"bar", "horizontal_bar"},
    "stacked_bar": {"bar", "horizontal_bar"},
    "bar": {"horizontal_bar", "grouped_bar", "stacked_bar"},
}

_AGG_ALIASES = {
    "sum": "SUM", "avg": "AVG", "count": "COUNT",
    "max": "MAX", "min": "MIN", "count_distinct": "COUNT_DISTINCT",
}


def _norm_agg(agg: str) -> str:
    a = str(agg or "").upper().replace("-", "_").replace(" ", "_")
    return _AGG_ALIASES.get(a, a)


# ----------------------------------------------------------------------
# 语义结构
# ----------------------------------------------------------------------

@dataclass
class SemanticQuery:
    """一次查询的语义指纹（三种路径归一后的统一口径）。"""

    metrics: list[dict] = field(default_factory=list)       # [{key}|{field,agg}]
    dimensions: list[dict] = field(default_factory=list)    # [{field, granularity?}]
    filters: list[dict] = field(default_factory=list)       # [{field, op, values|min|max}]
    top_n: dict | None = None                                # {limit, by, dir}
    query_type: str = "unknown"                              # metric_key | structured | raw_sql
    agent_calls: list[str] = field(default_factory=list)     # 证据：工具名序列


@dataclass
class SemanticResult:
    question_id: str
    query: str
    matched_metrics: bool = False
    matched_dims: bool = False
    matched_filters: bool = False
    matched_top_n: bool = False
    matched_chart: bool = False
    matched_answer: bool = False
    reachable: int = 0          # 应计分子项
    passed: int = 0             # 达成数
    details: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.passed / self.reachable if self.reachable else 0.0

    @property
    def overall(self) -> bool:
        # 语义达成：所有可判子项全部通过（分子项仍容错）
        return self.reachable > 0 and self.passed == self.reachable


# ----------------------------------------------------------------------
# 三路归一
# ----------------------------------------------------------------------

def _metric_fingerprint(m: dict) -> tuple:
    """指标指纹：key 展开或 (agg, field, distinct)。

    归一规则：
    - 显式 key/metric_key/metric_id 命中模板指标 → 展开为 (agg, 模板字段)。
    - field 位置写成指标 key 或名称（LLM 常见写法）→ 归一到同一指标口径。
    - 其余按 (agg, 字段) 归一；COUNT(*) 归 (COUNT, *)。
    """
    key = (m.get("metric_key") or m.get("metricKey") or m.get("metric_id")
           or m.get("key"))
    if key:
        if key in TEMPLATE_METRICS:
            agg, fld = TEMPLATE_METRICS[key]
            return _norm_agg(agg), fld, agg == "COUNT_DISTINCT"
        return ("KEY", key, False)
    field = m.get("field")
    agg = _norm_agg(m.get("agg") or "SUM")
    if field:
        fk = str(field).strip()
        # 字段位命中指标 key/名称 → 归一为指标 (agg, 模板字段)
        resolved = _resolve_metric_field(fk)
        if resolved is not None and (resolved[0] == agg or agg == "SUM"):
            agg, fld = resolved
            return _norm_agg(agg), fld, agg == "COUNT_DISTINCT"
    if agg == "COUNT_DISTINCT":
        return ("COUNT_DISTINCT", field, True)
    if agg == "COUNT" and not field:
        return ("COUNT", "*", False)
    return (agg, field, False)


def _resolve_metric_field(field: str):
    """field 是否命中指标 key/名称 → 返回 (agg, 模板字段)；否则 None。"""
    direct = _FIELD_ALIAS_TO_KEY.get(field) or _FIELD_ALIAS_TO_KEY.get(field.casefold())
    if direct in TEMPLATE_METRICS:
        return TEMPLATE_METRICS[direct]
    return None


def _dim_entries(dims: Any, buckets: dict | None = None) -> list[dict]:
    out: list[dict] = []
    for d in dims or []:
        if not isinstance(d, str):
            continue
        g = (buckets or {}).get(d)
        out.append({"field": d, "granularity": g or None})
    return out


def _range_filter(field: str, op: str, value: Any) -> dict:
    """op/value → 统一过滤语义（values 或 min/max）。"""
    op = str(op or "").lower()
    if op in ("eq",):
        return {"field": field, op: "", "values": None}
    if op == "in":
        vs = value if isinstance(value, list) else [value]
        return {"field": field, "op": "in", "values": [str(v) for v in vs]}
    if op in ("gt", "gte"):
        return {"field": field, "op": "min", "min": _filt_val(value), "min_exclusive": op == "gt"}
    if op in ("lt", "lte"):
        return {"field": field, "op": "max", "max": _filt_val(value), "max_exclusive": op == "lt"}
    if op == "between":
        lo, hi = (value or [None, None])[0], (value or [None, None])[1]
        return {"field": field, "op": "range", "min": _filt_val(lo), "max": _filt_val(hi)}
    return {"field": field, "op": "raw", "raw": str(op)}


def _filt_val(v) -> str:
    return str(v)


def qe_to_semantic(args: dict) -> SemanticQuery:
    """query_engine 结构化参数 → SemanticQuery。"""
    filters = []
    for fl in args.get("filters") or []:
        if not isinstance(fl, dict):
            continue
        filters.append(_range_filter(fl.get("field"), fl.get("op"), fl.get("value")))
    top_n = None
    srt = args.get("sort")
    limit = args.get("limit")
    if isinstance(srt, dict) or limit is not None:
        top_n = {
            "limit": limit if isinstance(limit, int) and limit > 0 else None,
            "by": (srt.get("field") if isinstance(srt, dict) else None)
            or "measure",
            "dir": (srt.get("order") if isinstance(srt, dict) else "desc") or "desc",
        }
    return SemanticQuery(
        metrics=[{**m} for m in (args.get("measures") or []) if isinstance(m, dict)],
        dimensions=_dim_entries(args.get("dimensions"), args.get("dimension_buckets")),
        filters=filters,
        top_n=top_n,
        query_type="structured",
    )


def metric_key_to_semantic(measures: list[dict]) -> SemanticQuery:
    """指标引用路径（Leader 决策后 query_engine 带 metric_key 的 measures）。"""
    return SemanticQuery(
        metrics=[{**m} for m in measures if isinstance(m, dict)],
        query_type="metric_key",
    )


# ---- 裸 SQL → 语义（SQLGlot，常见形态：聚合 + group by + where + limit/order）----

def _extract_sql_aggregates(node: Any) -> list[dict]:
    """提取 SELECT 中的聚合表达式。"""
    out: list[dict] = []
    for expr in node.expressions:
        e = expr
        if hasattr(e, "this") and type(e).__name__ in ("Alias", "AliasedColumn"):
            e = getattr(e, "this", None)
        if e is None:
            continue
        agg = e.find(__import__("sqlglot").exp.AggFunc) if e is not None else None
        if agg is None:
            continue
        agg_name = _norm_agg(type(agg).__name__)  # Sum→SUM
        col = agg.find(__import__("sqlglot").exp.Column)
        distinct = bool(agg.args.get("distinct"))
        if distinct or agg_name == "COUNT_DISTINCT" or (agg_name == "COUNT" and col is None):
            out.append({"agg": "COUNT_DISTINCT" if col else "COUNT",
                        "field": col.name if col else "*"})
        else:
            out.append({"agg": agg_name, "field": col.name if col else "*"})
    return out


def _extract_sql_dimensions(node: Any) -> list[dict]:
    out: list[dict] = []
    grp = node.find(__import__("sqlglot").exp.Group)
    if grp is None:
        return out
    for e in grp.expressions:
        e2 = e
        if hasattr(e2, "this") and type(e2).__name__ in ("Alias", "AliasedColumn"):
            e2 = getattr(e2, "this", None)
        if e2 is None:
            continue
        # date_trunc('month', col) → 维度 + 粒度
        col = e2.find(__import__("sqlglot").exp.Column)
        if col is not None:
            func = e2.find(__import__("sqlglot").exp.Func)
            granularity = None
            if func is not None and type(func).__name__ == "DateTrunc" and func.args.get("unit"):
                unit = func.args["unit"]
                granularity = unit.name if hasattr(unit, "name") else str(unit)
            out.append({"field": col.name, "granularity": granularity})
    return out


def _extract_sql_filters(tree: Any) -> list[dict]:
    """抽取 WHERE 中的字段比较（eq/in/gt/gte/lt/lte/between）。"""
    out: list[dict] = []
    where = tree.find(__import__("sqlglot").exp.Where)
    if where is None:
        return out
    for comp in where.find_all(__import__("sqlglot").exp.Binary):
        op_name = type(comp).__name__
        left = comp.find(__import__("sqlglot").exp.Column)
        if left is None:
            continue
        rhs = comp.args.get("expression")
        if rhs is None:
            continue
        val = None
        try:
            if isinstance(rhs, __import__("sqlglot").exp.Literal):
                val = rhs.name
            elif isinstance(rhs, list):
                val = [r.name for r in rhs]
            elif hasattr(rhs, "this"):
                val = getattr(rhs, "this", None)
                val = val.name if hasattr(val, "name") else val
        except Exception:
            val = None
        if val is None:
            continue
        mapped = {"EQ": "eq", "LT": "lt", "LTE": "lte", "GT": "gt", "GTE": "gte",
                  "In": "in"}.get(op_name)
        if mapped:
            out.append(_range_filter(left.name, mapped, val))
    return out


def _extract_sql_limit_order(tree: Any) -> dict | None:
    limit = tree.find(__import__("sqlglot").exp.Limit)
    limit_n = None
    if limit is not None and limit.args.get("expression") is not None:
        try:
            limit_n = int(limit.args["expression"].name)
        except Exception:
            limit_n = None
    order = tree.find(__import__("sqlglot").exp.Order)
    direction = "desc"
    if order is not None and order.expressions:
        first = order.expressions[0]
        if hasattr(first, "args") and first.args.get("desc"):
            direction = "desc"
        elif first is not None and type(first).__name__ == "Ordered":
            direction = "desc" if first.args.get("desc") else "asc"
    if limit_n is None and order is None:
        return None
    return {"limit": limit_n, "by": "measure", "dir": direction}


def raw_sql_to_semantic(sql: str) -> SemanticQuery | None:
    """query_sql 裸 SQL → SemanticQuery（解析失败返回 None，判分时该子项不计）。"""
    try:
        sqlglot = __import__("sqlglot")
        tree = sqlglot.parse_one(sql, read="duckdb")
        select = tree.find(sqlglot.exp.Select)
        if select is None:
            return None
    except Exception:
        return None
    return SemanticQuery(
        metrics=_extract_sql_aggregates(select),
        dimensions=_extract_sql_dimensions(select),
        filters=_extract_sql_filters(select),
        top_n=_extract_sql_limit_order(select),
        query_type="raw_sql",
    )


# ----------------------------------------------------------------------
# 从 attempt.events 归一用户实际查询（三条路都收集，合并为最终查询语义）
# ----------------------------------------------------------------------

def extract_user_query(events: list[dict]) -> SemanticQuery:
    """取 Agent 的最后一次有效查询语义（query_engine 结构化 或 query_sql 裸 SQL）。

    仅当对应工具的**执行结果成功**时才认定为有效查询：失败/报错的事件虽记录了
    tool_call 意图，但系统并未真正取到数，不应作为达标证据（否则掩盖执行缺陷）。
    """
    qe: SemanticQuery | None = None
    raw: SemanticQuery | None = None
    calls: list[str] = []
    pending: dict[str, list[list]] = {}   # name -> [[args, consumed], ...]
    for e in events:
        t = e.get("type")
        name = e.get("name", "")
        if t == "tool_call" and name in ("query_engine", "query_sql"):
            args = e.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(name)
            pending.setdefault(name, []).append([args, False])
        elif t == "tool_result" and name in ("query_engine", "query_sql"):
            ok = _tool_result_ok(e)
            queue = pending.get(name)
            if not queue:
                continue
            # 配对到最早的未消费调用
            for rec in queue:
                if not rec[1]:
                    rec[1] = True
                    if not ok:
                        break
                    args = rec[0]
                    if name == "query_engine":
                        sq = qe_to_semantic(args)
                        if sq.metrics or sq.dimensions:
                            qe = sq
                    else:
                        sql = args.get("sql") or args.get("query")
                        if isinstance(sql, str) and sql.strip():
                            parsed = raw_sql_to_semantic(sql)
                            if parsed is not None and (parsed.metrics or parsed.dimensions):
                                raw = parsed
                    break
    final = qe if qe is not None else raw
    if final is not None:
        final.agent_calls = calls
        return final
    return SemanticQuery(agent_calls=calls)


def _tool_result_ok(e: dict) -> bool:
    """判断工具执行结果是否成功。"""
    ok = e.get("ok")
    if ok is not None:
        return bool(ok)
    status = e.get("status")
    if status is not None:
        return status not in (False, "error", "failed", "failure")
    res = e.get("result")
    if isinstance(res, str):
        s = res.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                return False
            if isinstance(obj, dict):
                if "row_count" in obj:          # query_engine 成功结果
                    return True
                if obj.get("ok") is False or obj.get("error"):
                    return False
                if "rows" in obj or "columns" in obj:
                    return True
                return True
            return True
        low = s.lower()
        if low.startswith(("error", "traceback", "conversion error", "binder error",
                           "catalog error", "exception")):
            return False
        return bool(s)                          # 普通文本结果视为成功
    if isinstance(res, dict):
        if res.get("ok") is False or res.get("error"):
            return False
        if "row_count" in res or "rows" in res or "columns" in res:
            return True
        return True
    return False


def iter_successful_calls(events: list[dict], name: str):
    """按名字对 tool_call/tool_result 顺序配对，产出 (args, ok) 的成功调用。

    事件流中 tool_call 与紧跟的 tool_result 同名成对出现（同步执行）；
    用队列把同名 result 配对到最早的未消费 call，避免只凭意图误判为达标。
    """
    pending: list[tuple[dict, int]] = []      # (args, index)
    consumed: set[int] = set()
    for e in events:
        t = e.get("type")
        if t == "tool_call" and e.get("name") == name:
            args = e.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            pending.append((args, len(pending)))
        elif t == "tool_result" and e.get("name") == name:
            ok = _tool_result_ok(e)
            # 配对到最早的未消费 call
            for (args, idx) in pending:
                if idx not in consumed:
                    consumed.add(idx)
                    if ok:
                        yield args, True
                    break
    return


def _parse_tool_result(e: dict) -> dict:
    """解析 tool_result 的 result 字段（兼容字符串/字典）。"""
    res = e.get("result")
    if isinstance(res, dict):
        return res
    if isinstance(res, str):
        try:
            return json.loads(res)
        except json.JSONDecodeError:
            return {}
    return {}


def _executed_canvas_blocks(events: list[dict]) -> list[dict]:
    """已成功落块的画布块的**执行语义**（block.queryConfig，含 resolve_measures 归一后的 measures）。

    判分以系统真正执行的口径为准，而非 LLM 手写的原始参数——这样无论未来新增哪个指标、
    或 resolve_measures 做什么字段→指标升级，判分器都自动对齐，不需逐一补特例。
    """
    out: list[dict] = []
    for e in events:
        if e.get("type") != "tool_result" or e.get("name") != "add_chart_block":
            continue
        if not _tool_result_ok(e):
            continue
        res = _parse_tool_result(e)
        block = (res.get("canvas_action") or {}).get("block") or {}
        qc = block.get("queryConfig") or {}
        if qc:
            out.append(qc)
    return out


def extract_canvas_metrics(events: list[dict]) -> list[dict]:
    """画布路径：提取指标（add_chart_block 执行后 queryConfig + update_chart_block 成功后 patch）。

    - 只统计落块/改图成功（工具返回 ok）；
    - add 读取 resolve_measures 归一后的 measures（可能含 metric_key），与生产执行完全一致；
    - update 读取其 patch.measures（修改后的最终度量），改图类任务只有计入才算口径正确。
    """
    out: list[dict] = []
    for qc in _executed_canvas_blocks(events):
        for m in (qc.get("measures") or []):
            if isinstance(m, dict):
                out.append({**m})
    for e in events:
        if e.get("type") != "tool_result" or e.get("name") != "update_chart_block":
            continue
        if not _tool_result_ok(e):
            continue
        res = _parse_tool_result(e)
        patch = (res.get("canvas_action") or {}).get("patch") or {}
        for m in (patch.get("measures") or []):
            if isinstance(m, dict):
                out.append({**m})
    return out


def iter_successful_chart_types(events: list[dict]) -> list[str]:
    """已成功落块的图表类型：add_chart_block 的成功块 + update_chart_block 成功后改成的类型。

    update 修改的是已有块，其 patch.chartType 是该块最终生效的类型——改图类任务
    （用户改已有图、调粒度/换类型）只有计入 update 类型，判分才反映真实画布结果。
    """
    out: list[str] = []
    for args, ok in iter_successful_calls(events, "add_chart_block"):
        ct = args.get("chart_type") or args.get("chartType")
        if ct:
            out.append(str(ct))
    for args, ok in iter_successful_calls(events, "update_chart_block"):
        ct = args.get("chart_type") or args.get("chartType")
        if ct:
            out.append(str(ct))
    return out


# ----------------------------------------------------------------------
# 判分：语义覆盖（期望 ⊆ Agent）
# ----------------------------------------------------------------------

def _metrics_match(expected: list[dict], actual: list[dict]) -> tuple[bool, str]:
    if not expected:
        return True, "metrics: 期望无指标约束"
    exp_fps = [_metric_fingerprint(m) for m in expected]
    act_fps = [_metric_fingerprint(m) for m in actual]
    missing = []
    for i, fp in enumerate(exp_fps):
        if fp not in act_fps:
            missing.append(f"[{json.dumps(expected[i], ensure_ascii=False)}]")
    if missing:
        return False, "metrics 缺失: " + "; ".join(missing)
    return True, "metrics✓"


def _dims_match(expected: list[dict], actual: list[dict]) -> tuple[bool, str]:
    if not expected:
        return True, "dims: 期望无维度约束"
    missing = []
    for d in expected:
        found = any(
            a.get("field") == d.get("field")
            and (a.get("granularity") or None) == (d.get("granularity") or None)
            for a in actual
        )
        if not found:
            missing.append(d.get("field") + (f"@{d.get('granularity')}" if d.get("granularity") else ""))
    if missing:
        return False, "维度缺失: " + "; ".join(missing)
    return True, "dims✓"


def _range_covers(expected: dict, actual: dict) -> bool:
    """actual 覆盖 expected（期望范围 ⊆ 实际范围）。仅比较双方都写了的具体界。"""
    ev, av = expected, actual
    if ev.get("op") == "in" and av.get("op") == "in":
        return set(ev.get("values") or []) <= set(av.get("values") or [])

    def num(v) -> float | None:
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return None

    if ev.get("min") is not None:
        em, am = num(ev["min"]), num(av.get("min")) if av.get("min") is not None else None
        if am is None or em < am:      # 期望下界更早而实际更晚 → 实际未覆盖
            return False
    if ev.get("max") is not None:
        em, am = num(ev["max"]), num(av.get("max")) if av.get("max") is not None else None
        if am is None or em > am:      # 期望上界更晚而实际更早 → 实际未覆盖
            return False
    return True


def _filters_match(expected: list[dict], actual: list[dict]) -> tuple[bool, str]:
    if not expected:
        return True, "filters: 期望无过滤约束"
    missing = []
    for e in expected:
        a = next((x for x in actual if x.get("field") == e.get("field")), None)
        if a is None:
            missing.append(f"{e.get('field')}({e.get('op', 'raw')})")
            continue
        if not _range_covers(e, a):
            missing.append(f"{e.get('field')}(期望{e.get('op')} 实际{a.get('op')})")
    if missing:
        return False, "过滤缺失: " + "; ".join(missing)
    return True, "filters✓"


def _top_n_match(expected: dict | None, actual: dict | None) -> tuple[bool, str]:
    if not expected:
        return True, "top_n: 期望无语序约束"
    if not actual:
        return False, "top_n 缺失（期望按度量排序限量）"
    exp_dir = (expected.get("dir") or "desc")
    act_dir = (actual.get("dir") or "desc")
    if exp_dir != act_dir:
        return False, f"top_n 排序方向不符(期望{exp_dir} 实际{act_dir})"
    if expected.get("limit") and actual.get("limit") is not None and actual["limit"] < expected["limit"]:
        return False, f"top_n 限量不足(期望≥{expected['limit']} 实际{actual['limit']})"
    return True, "top_n✓"


def _chart_match(expected: str | list, actual: str) -> tuple[bool, str]:
    exp = expected if isinstance(expected, list) else [expected]
    exp = [x for x in exp if x]
    if not exp:
        return True, "chart: 期望无图表约束"
    if actual in exp:
        return True, f"chart✓({actual})"
    for e, ok in CHART_EQUIV.items():
        if actual == e and ok & set(exp):
            return True, f"chart≈({actual}~{','.join(ok & set(exp))})"
    return False, f"chart 不符(期望{','.join(exp)} 实际{actual})"


def _answer_match(expected_keys: list[str], final_response: str) -> tuple[bool, str]:
    if not expected_keys:
        return True, "answer: 期望无回答约束"
    if not final_response:
        return False, "回答为空"
    hit = sum(1 for k in expected_keys if k in final_response)
    if hit / len(expected_keys) >= 0.6:
        return True, f"answer✓({hit}/{len(expected_keys)})"
    return False, f"回答缺要点({expected_keys})"


# ----------------------------------------------------------------------
# 对话路径判分
# ----------------------------------------------------------------------

def judge_semantic(question: dict, user_query: SemanticQuery, *, chart_type: str = "",
                   final_response: str = "") -> SemanticResult:
    exp = question.get("expected") or {}
    r = SemanticResult(question_id=str(question.get("id", "?")), query=str(question.get("query", "")))
    if not user_query.metrics:
        r.details.append("query: 未解析出查询语义（Agent 未走查询或解析失败，本维度不计）")
    else:
        r.details.append(f"query_type={user_query.query_type} calls={user_query.agent_calls}")

    # metrics
    r.matched_metrics, d = _metrics_match(exp.get("metrics") or [], user_query.metrics)
    r.reachable += 1
    if r.matched_metrics:
        r.passed += 1
    r.details.append(d)

    # dimensions / granularity
    r.matched_dims, d = _dims_match(exp.get("dimensions") or [], user_query.dimensions)
    r.reachable += 1
    if r.matched_dims:
        r.passed += 1
    r.details.append(d)

    # filters（含时间口径）
    r.matched_filters, d = _filters_match(exp.get("filters") or [], user_query.filters)
    r.reachable += 1
    if r.matched_filters:
        r.passed += 1
    r.details.append(d)

    # top_n
    r.matched_top_n, d = _top_n_match(exp.get("top_n"), user_query.top_n)
    r.reachable += 1
    if r.matched_top_n:
        r.passed += 1
    r.details.append(d)

    # chart
    if chart_type:
        r.matched_chart, d = _chart_match(exp.get("chart", {}).get("types") if isinstance(exp.get("chart"), dict) else exp.get("chart_type"), chart_type)
        r.reachable += 1
        if r.matched_chart:
            r.passed += 1
        r.details.append(d)

    # answer
    r.matched_answer, d = _answer_match(exp.get("answer_keys") or [], final_response)
    r.reachable += 1
    if r.matched_answer:
        r.passed += 1
    r.details.append(d)

    return r


# ----------------------------------------------------------------------
# 画布路径判分（画布题：指标引用正确性 + 报告要素）
# ----------------------------------------------------------------------

def _chart_covered(act_set: set[str], expected: str) -> bool:
    """expected 是否被已落图表类型覆盖（类型本身或近邻等价，如 grouped_bar≈bar）。"""
    if expected in act_set:
        return True
    neighbors = set()
    # expected 自身声明的等价集合 + 其反向出现在已落类型里的近邻
    neighbors |= CHART_EQUIV.get(expected, set())
    for act in act_set:
        if expected in CHART_EQUIV.get(act, set()):
            neighbors.add(act)
    return bool(act_set & neighbors)


def _update_used(events: list[dict]) -> bool:
    """是否出现过成功的 update_chart_block 调用（改图链路证据）。"""
    for e in events:
        if e.get("type") != "tool_result" or e.get("name") != "update_chart_block":
            continue
        if _tool_result_ok(e):
            return True
    return False


def judge_canvas_semantic(question: dict, block_metrics: list[dict], *,
                          block_chart_types: list[str] | None = None,
                          block_count: int = 0,
                          final_response: str = "",
                          events: list[dict] | None = None) -> SemanticResult:
    exp = question.get("expected") or {}
    r = SemanticResult(question_id=str(question.get("id", "?")), query=str(question.get("query", "")))

    # 指标引用正确性：画布块 measures 与期望指标集匹配
    r.matched_metrics, d = _metrics_match(exp.get("metrics") or [], block_metrics)
    r.reachable += 1
    if r.matched_metrics:
        r.passed += 1
    r.details.append("metrics(画布引用): " + d)

    # 期望图表类型覆盖（块 chart_type，允许近邻等价如 grouped_bar≈bar）
    # match: all（默认）= 报告式多图，期望类型全部覆盖；match: any = 单图/改图题，任一类型命中即可
    exp_types = (exp.get("chart") or {}).get("types") or []
    match_mode = (exp.get("chart") or {}).get("match") or "all"
    act_types = list(dict.fromkeys(block_chart_types or []))
    if exp_types:
        act_set = set(act_types)
        if match_mode == "any":
            missing = [t for t in exp_types if not (_chart_covered(act_set, t))]
            r.matched_chart = any(_chart_covered(act_set, t) for t in exp_types)
        else:
            missing = [t for t in exp_types if not (_chart_covered(act_set, t))]
            r.matched_chart = not missing
        r.reachable += 1
        if r.matched_chart:
            r.passed += 1
        r.details.append(f"chart_types{'✓' if r.matched_chart else '✗'} 期望{exp_types} 实际{act_types}"
                         + (f" 缺{missing}" if missing else ""))

    # 图表数量（报告体征）
    min_charts = (exp.get("canvas") or {}).get("min_charts") or 1
    r.matched_top_n = block_count >= min_charts
    r.reachable += 1
    if r.matched_top_n:
        r.passed += 1
    r.details.append(f"block_count{'✓' if r.matched_top_n else '✗'} 实际{block_count} 期望≥{min_charts}")

    # 修改已有图（update_used）：只对期望明确要求"用 update 改图"的题判
    if (exp.get("canvas") or {}).get("update_used"):
        r.matched_filters = _update_used(events or [])
        r.reachable += 1
        if r.matched_filters:
            r.passed += 1
        r.details.append(f"update_used{'✓' if r.matched_filters else '✗'} (期望出现过 update_chart_block 成功调用)")

    # 叙事/回答要素
    r.matched_answer, d = _answer_match(exp.get("answer_keys") or [], final_response)
    r.reachable += 1
    if r.matched_answer:
        r.passed += 1
    r.details.append(d)
    return r