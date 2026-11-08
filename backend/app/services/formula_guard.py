# -*- coding: utf-8 -*-
"""指标 formula 的 **AST 白名单**校验器（安全边界，fail-closed）。

为什么从正则换成 AST
--------------------
旧实现（`metric_service._validate_derived_expression`）是手写字符扫描 + 正则白名单：
它只能表达"聚合 + 四则"，于是 `CASE WHEN` / `COALESCE` / `CAST` 这类**纯表达式**一律被拒；
而一旦为它们逐个打正则补丁，就会变成"用字符串模式去猜 SQL 语法"——漏一个引号/换行
就是注入面。sqlglot 已经把语法树解析好了，**白名单应该建在节点类型上**。

边界（对齐 docs/research/08-metric-semantic-layer-dsl.md 的结论）
----------------------------------------------------------------
调研结论：主流语义层（dbt MetricFlow / Cube / LookML / AtScale / Malloy）**没有任何一家**
允许在"指标表达式"层写任意 SQL；分界线不是"能不能聚合"，而是"聚合之外还能不能有查询结构"。

允许（表达式层）：
  · 聚合：SUM/AVG/COUNT/MIN/MAX/MEDIAN/STDDEV* / VARIANCE* / 分位数 / APPROX_* /
    ANY_VALUE / COUNT_IF；`COUNT(DISTINCT x)`、`COUNT(*)`；
  · 四则与括号：+ - * / % 与一元负号；
  · 条件：CASE WHEN ... THEN ... END、IF(cond, a, b)；
  · 空值处理：COALESCE / NULLIF / IFNULL（sqlglot 把 IFNULL 归一成 COALESCE）；
  · 类型转换：CAST / TRY_CAST（含 DECIMAL(18,2) 这类参数）；
  · 标量函数：ROUND/ABS/CEIL(ING)/FLOOR/POWER/SQRT/LN/LOG/EXP/GREATEST/LEAST/MOD；
  · 引用其他指标：`metric("key")`；
  · 模板占位符：`{{field}}`（解析前替换成哨兵标识符，错误信息里再还原）。

**禁止**（本轮不做，别放开）：
  · 子查询 / 任何查询结构（FROM、JOIN、UNION、WITH、SELECT 嵌套）——等于引入新的数据来源；
  · 窗口函数（OVER(...)）——占比/累计应由引擎内部生成（未来 `share_of_total` 查询期开关），
    不允许用户在指标口径里手写；
  · DDL/DML、多条语句、SQL 注释；
  · 未知函数；裸字段/纯常量作为整个表达式（公式必须表达一个聚合值或对其它指标的引用）。

未做（不属于本轮）：指标级条件口径（编译成条件聚合的 filters）、比率显式类型（分子/分母）。
本模块只回答"表达式里能写什么"，不改变"比率怎么建模"——放开四则不会妨碍以后加 ratio 类型。

用法
----
    from app.services.formula_guard import FormulaRejected, validate_formula
    try:
        validate_formula('SUM(CASE WHEN "status"='paid' THEN "amount" END)')
    except FormulaRejected as exc:
        print(exc.reason)   # 中文可读原因
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

# ── 对外常量（metric_service / 测试共用）──────────────────────────────────

DIALECT = "duckdb"
_MAX_FORMULA_CHARS = 2000
# 包装解析用的投影别名：解析成功后**只校验它的 inner 表达式**
_PROJECTION_ALIAS = "__lvco_formula__"

# 聚合函数名（归一化后；sqlglot 会映射别名，这里把"用户写法"和"规范名"都收进来）
ALLOWED_AGG_SQL_NAMES: frozenset[str] = frozenset({
    "SUM", "AVG", "COUNT", "MIN", "MAX",
    "MEDIAN",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
    "VARIANCE", "VARIANCE_POP", "VARIANCE_SAMP",
    "VAR_POP", "VAR_SAMP",                         # 用户写法（部分版本不归一）
    "QUANTILE_CONT", "QUANTILE_DISC",
    "PERCENTILE_CONT", "PERCENTILE_DISC",
    "APPROX_QUANTILE",
    "APPROX_COUNT_DISTINCT", "APPROX_DISTINCT",    # sqlglot 把前者的规范名记作 APPROX_DISTINCT
    "ANY_VALUE",
    "COUNT_IF",
})

# 标量函数名（归一化后）
ALLOWED_SCALAR_SQL_NAMES: frozenset[str] = frozenset({
    "ROUND", "ABS", "CEIL", "CEILING", "FLOOR",
    "POWER", "SQRT", "LN", "LOG", "EXP",
    "GREATEST", "LEAST", "IFNULL", "MOD",
})

# 表达式结构函数（不是"可调用的业务函数"，但必须以 Func 节点的形式出现）
_STRUCT_FUNC_NAMES: frozenset[str] = frozenset({
    "CASE", "IF", "COALESCE", "NULLIF", "CAST", "TRY_CAST",
})

_PERCENTILE_NAMES: frozenset[str] = frozenset({
    "QUANTILE_CONT", "QUANTILE_DISC", "PERCENTILE_CONT", "PERCENTILE_DISC",
})

_METRIC_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# 模板占位符 {{field}}：与 metric_service.extract_metric_fields 同一形状
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SENTINEL_FMT = "__lvco_ph_{i}__"
_SENTINEL_RE = re.compile(r"__lvco_ph_(\d+)__")


class FormulaRejected(ValueError):
    """指标 formula 不符合表达式白名单。

    `reason`：中文可读原因（同时作为 str(exc)）；`formula`：原始公式（占位符保持原样）。
    """

    def __init__(self, reason: str, *, formula: str = "") -> None:
        self.reason = reason
        self.formula = formula
        super().__init__(reason)


# ── 节点白名单 ────────────────────────────────────────────────────────────

def _classes(*names: str) -> tuple[type, ...]:
    """按名字取 sqlglot 类；不同版本缺某个类就跳过（避免 import 期崩）。"""
    out: list[type] = []
    for name in names:
        cls = getattr(exp, name, None)
        if isinstance(cls, type):
            out.append(cls)
    return tuple(out)


# 纯表达式层的结构/运算节点（不引入任何查询结构）
_ALLOWED_NODE_TYPES: tuple[type, ...] = _classes(
    "Column", "Identifier", "Literal", "Null", "Boolean",
    "Paren",
    "Add", "Sub", "Mul", "Div", "Mod", "Neg",
    "DataType", "DataTypeParam",
    # 条件里必需的比较/逻辑（CASE WHEN、COUNT_IF）：只作用在表达式上，不构成查询结构
    "EQ", "NEQ", "GT", "GTE", "LT", "LTE",
    "And", "Or", "Not", "Is", "Between",
)

# 需要"上下文"才允许的节点（* / DISTINCT / 聚合的结构包装）
_STAR = _classes("Star")
_DISTINCT = _classes("Distinct")
_IGNORE_NULLS = _classes("IgnoreNulls")
_WITHIN_GROUP = _classes("WithinGroup")
_ORDER_LIKE = _classes("Order", "Ordered")
_ALLOWED_CONTEXTUAL_TYPES: tuple[type, ...] = (
    _STAR + _DISTINCT + _IGNORE_NULLS + _WITHIN_GROUP + _ORDER_LIKE
)

# 明确给出友好提示的禁用节点（其余未知节点统一按"不支持的语法结构"拒绝）
_FORBIDDEN_HINTS: dict[type, str] = {
    cls: hint
    for cls, hint in (
        (getattr(exp, "Window", None), "不支持窗口函数 OVER(...)（占比/累计由引擎内部生成）"),
        (getattr(exp, "Subquery", None), "不支持子查询"),
        (getattr(exp, "Select", None), "不支持子查询/嵌套查询"),
        (getattr(exp, "Union", None), "不支持 UNION"),
        (getattr(exp, "With", None), "不支持 CTE（WITH）"),
        (getattr(exp, "Join", None), "不支持 JOIN"),
        (getattr(exp, "Table", None), "不支持 FROM/表引用"),
        (getattr(exp, "Command", None), "不支持命令语句"),
        (getattr(exp, "Insert", None), "不支持 DDL/DML"),
        (getattr(exp, "Update", None), "不支持 DDL/DML"),
        (getattr(exp, "Delete", None), "不支持 DDL/DML"),
        (getattr(exp, "Create", None), "不支持 DDL/DML"),
        (getattr(exp, "Drop", None), "不支持 DDL/DML"),
        (getattr(exp, "Alter", None), "不支持 DDL/DML"),
    )
    if isinstance(cls, type)
}

# 根 SELECT 上不允许出现的子句（出现即说明公式里混进了查询结构）。
# 注意 sqlglot 的 arg 键名带下划线：FROM 是 "from_"，WITH 是 "with_"
# （实测 v30：SELECT 1 FROM t 的 args 里只有 "from_"，没有 "from"）。
_FORBIDDEN_SELECT_ARGS = frozenset({
    "from_", "from", "joins", "where", "group", "having", "order", "limit",
    "offset", "fetch", "with_", "with", "qualify", "distinct", "kind",
    "operation_modifiers", "laterals", "pivots", "windows", "sample", "sort",
})

# 报错时给人看的子句名（键名 → SQL 关键字）
_SELECT_ARG_DISPLAY = {
    "from_": "FROM", "from": "FROM", "joins": "JOIN", "where": "WHERE",
    "group": "GROUP BY", "having": "HAVING", "order": "ORDER BY", "limit": "LIMIT",
    "offset": "OFFSET", "fetch": "FETCH", "with_": "WITH", "with": "WITH",
    "qualify": "QUALIFY", "distinct": "DISTINCT", "kind": "KIND",
    "operation_modifiers": "OPERATION_MODIFIERS", "laterals": "LATERAL",
    "pivots": "PIVOT", "windows": "WINDOW", "sample": "SAMPLE", "sort": "SORT",
}


# ── 小工具 ────────────────────────────────────────────────────────────────

def _restore_placeholders(text: str, names: list[str]) -> str:
    """把哨兵标识符还原成 {{field}}，让错误信息读得懂。"""
    def _sub(m: re.Match) -> str:
        idx = int(m.group(1))
        return "{{" + names[idx] + "}}" if 0 <= idx < len(names) else m.group(0)

    return _SENTINEL_RE.sub(_sub, text or "")


def _func_sql_name(node: exp.Func) -> str:
    """取函数名的归一化形式（sqlglot 会把别名映射成规范名，如 VAR_POP→VARIANCE_POP）。"""
    try:
        return str(node.sql_name() or "").upper()
    except Exception:  # noqa: BLE001 - 取不到名字就当未知函数（fail-closed）
        return ""


def _node_func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(getattr(node, "name", "") or "").upper()
    if isinstance(node, exp.Func):
        return _func_sql_name(node)
    return ""


def _is_metric_ref(node: exp.Expression) -> bool:
    return isinstance(node, exp.Anonymous) and _node_func_name(node) == "METRIC"


def _walk(root: exp.Expression):
    """DFS 产出 (node, ancestors)；ancestors 是**从根到父**的元组（不含自身）。"""
    stack: list[tuple[exp.Expression, tuple[exp.Expression, ...]]] = [(root, ())]
    while stack:
        node, ancestors = stack.pop()
        yield node, ancestors
        for child in node.iter_expressions():
            stack.append((child, ancestors + (node,)))


# ── 规则：函数 / 上下文 / 列 ──────────────────────────────────────────────

def _validate_metric_ref(node: exp.Anonymous, formula: str) -> None:
    args = list(node.expressions)
    if len(args) != 1:
        raise FormulaRejected(
            'metric("key") 需要恰好一个参数，例如 metric("sales_amount")', formula=formula
        )
    arg = args[0]
    key: str | None = None
    if isinstance(arg, exp.Literal) and arg.is_string:
        key = str(arg.this)
    elif isinstance(arg, exp.Column) and not (arg.table or arg.db or arg.catalog):
        # sqlglot 把 metric("key") 的双引号参数解析成"带引号的标识符"（Column）
        key = arg.name
    if not key or not _METRIC_KEY_RE.fullmatch(key):
        raise FormulaRejected(
            'metric("key") 的参数必须是字符串字面量形式（指标 key）', formula=formula
        )


def _validate_function(node: exp.Func, formula: str) -> None:
    """函数节点：聚合 / 标量 / 结构函数三张名单；metric 引用单独校验。"""
    if _is_metric_ref(node):
        _validate_metric_ref(node, formula)  # type: ignore[arg-type]
        return

    name = _node_func_name(node)
    if name in ALLOWED_AGG_SQL_NAMES or name in ALLOWED_SCALAR_SQL_NAMES:
        return
    if name in _STRUCT_FUNC_NAMES:
        return
    raise FormulaRejected(
        f"不支持函数 {name or type(node).__name__}（可用聚合/标量函数见指标 formula 说明）",
        formula=formula,
    )


def _contextual_reason(node: exp.Expression, ancestors: tuple[exp.Expression, ...]) -> str | None:
    """上下文敏感节点的规则；返回拒绝原因，None 表示通过。"""
    parent = ancestors[-1] if ancestors else None

    if isinstance(node, _STAR):
        if not isinstance(parent, exp.Count):
            return "只允许 COUNT(*) 使用 *"
        return None

    if isinstance(node, _DISTINCT):
        if not isinstance(parent, exp.Count):
            return "DISTINCT 只允许出现在 COUNT(DISTINCT ...) 里"
        return None

    if isinstance(node, _IGNORE_NULLS):
        names = {_node_func_name(node.this), _node_func_name(parent) if parent is not None else ""}
        if "ANY_VALUE" not in names:
            return "不支持的聚合包装（仅 ANY_VALUE 允许）"
        return None

    if isinstance(node, _WITHIN_GROUP):
        if _node_func_name(node.this) not in _PERCENTILE_NAMES:
            return "WITHIN GROUP 只允许用在分位数聚合（PERCENTILE_CONT 等）内部"
        return None

    if isinstance(node, _ORDER_LIKE):
        if not any(isinstance(a, _WITHIN_GROUP) for a in ancestors):
            return "不支持 ORDER BY（仅分位数聚合的 WITHIN GROUP 内允许）"
        return None

    return None


# ── 对外入口 ──────────────────────────────────────────────────────────────

def validate_formula(formula: str) -> None:
    """校验指标 formula；不合法抛 `FormulaRejected`（中文可读原因）。

    流程：语法前置检查（空/长度/注释/多语句）→ 占位符换哨兵 → 包成
    `SELECT <expr> AS <别名>` 解析 → 根/投影/子句结构校验 → 逐节点白名单 →
    结构性约束（裸字段、聚合存在性）。**任何解析异常都视为拒绝**（fail-closed）。
    """
    raw = str(formula or "").strip()
    if not raw:
        raise FormulaRejected("指标 formula 不能为空")
    if len(raw) > _MAX_FORMULA_CHARS:
        raise FormulaRejected(f"指标 formula 过长（超过 {_MAX_FORMULA_CHARS} 字符）")

    # 注释与多语句在**解析前**拒：包裹后的解析错误信息不可读，
    # 而且不该依赖解析器对注释的宽容度（版本间有差异）。
    if ";" in raw:
        raise FormulaRejected("不支持多条语句（formula 里不能有 ';'）", formula=raw)
    if "--" in raw or "/*" in raw or "*/" in raw:
        raise FormulaRejected("不支持 SQL 注释", formula=raw)

    # 占位符 → 哨兵标识符（保证 SUM({{amount}}) 这类模板公式能被解析）
    names: list[str] = []
    body = raw
    for m in _PLACEHOLDER_RE.finditer(raw):
        idx = len(names)
        names.append(m.group(1))
        body = body.replace(m.group(0), _SENTINEL_FMT.format(i=idx))
    if "{{" in body or "}}" in body:
        raise FormulaRejected("占位符格式不合法（应为 {{field}}）", formula=raw)

    try:
        parsed = sqlglot.parse(f"SELECT {body} AS {_PROJECTION_ALIAS}", dialect=DIALECT)
    except Exception as exc:  # noqa: BLE001 - 解析失败一律 fail-closed
        raise FormulaRejected(
            "无法解析为合法的指标表达式（语法错误，或含不支持的查询结构）", formula=raw
        ) from exc

    trees = [t for t in (parsed or []) if t is not None]
    if len(trees) != 1:
        raise FormulaRejected("不支持多条语句", formula=raw)

    root = trees[0]
    if not isinstance(root, exp.Select):
        raise FormulaRejected("公式必须是一个表达式（不支持查询语句）", formula=raw)

    for key in sorted(_FORBIDDEN_SELECT_ARGS):
        if root.args.get(key):
            display = _SELECT_ARG_DISPLAY.get(key, key.upper())
            raise FormulaRejected(
                f"不支持查询结构（{display}）：指标公式只能是聚合表达式", formula=raw
            )

    if len(root.expressions) != 1:
        raise FormulaRejected("公式必须恰好是一个表达式", formula=raw)
    projection = root.expressions[0]
    if not isinstance(projection, exp.Alias) or str(projection.alias or "") != _PROJECTION_ALIAS:
        raise FormulaRejected("公式结构异常（解析结果不是单个表达式）", formula=raw)

    body_expr = projection.this
    if body_expr is None:
        raise FormulaRejected("指标 formula 不能为空", formula=raw)

    # 逐节点校验：白名单外的任何节点都拒绝
    columns: list[tuple[exp.Expression, tuple[exp.Expression, ...]]] = []
    agg_nodes: list[exp.Expression] = []

    for node, ancestors in _walk(body_expr):
        if isinstance(node, _ALLOWED_NODE_TYPES):
            if isinstance(node, exp.Column):
                if node.table or node.db or node.catalog:
                    raise FormulaRejected(
                        "不支持带表前缀的列引用：" + node.sql(dialect=DIALECT), formula=raw
                    )
                columns.append((node, ancestors))
            continue

        if isinstance(node, _ALLOWED_CONTEXTUAL_TYPES):
            reason = _contextual_reason(node, ancestors)
            if reason:
                raise FormulaRejected(reason, formula=raw)
            continue

        if isinstance(node, exp.Func):
            _validate_function(node, raw)
            name = _node_func_name(node)
            if _is_metric_ref(node) or name in ALLOWED_AGG_SQL_NAMES:
                agg_nodes.append(node)
            continue

        hint = None
        for cls, cls_hint in _FORBIDDEN_HINTS.items():
            if isinstance(node, cls):
                hint = cls_hint
                break
        raise FormulaRejected(hint or f"不支持的语法结构：{type(node).__name__}", formula=raw)

    # 公式必须表达"一个聚合值"或"对其它指标的引用"：挡掉纯常量 / 裸字段
    if not agg_nodes:
        raise FormulaRejected(
            '公式必须包含聚合函数或 metric("key") 引用'
            "（不允许裸字段或纯常量作为指标口径）",
            formula=raw,
        )

    # 每个列引用都必须位于聚合（或 metric 引用）内部：
    # 这挡掉 "amount" / "amount * 2" 这类"没有聚合"的口径，同时允许条件聚合里的列。
    for col, ancestors in columns:
        if not any(agg in ancestors for agg in agg_nodes):
            raise FormulaRejected(
                f'列 {col.name} 必须写在聚合函数内部（例如 SUM("{col.name}")）',
                formula=raw,
            )


def is_formula_allowed(formula: str) -> bool:
    """布尔版校验（不抛异常）：UI 标红/批量体检用。"""
    try:
        validate_formula(formula)
    except FormulaRejected:
        return False
    return True
