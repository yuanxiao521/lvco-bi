# -*- coding: utf-8 -*-
"""指标 formula 的 AST 白名单校验（formula_guard）与 metric_service 接线。

背景：旧实现用手写正则只放行「单一聚合 + 四则」，于是 CASE WHEN / COALESCE / CAST
这类**纯表达式**全被误拒（条件口径写不了、安全除法写不了）。本文件把新的边界钉死：

- 允许：聚合、四则、CASE WHEN/IF、COALESCE/NULLIF、CAST/TRY_CAST、标量函数、
  metric("key") 引用、{{field}} 模板占位符；
- 拒绝：子查询与任何查询结构（FROM/JOIN/UNION/WITH）、窗口函数、DDL/DML、
  多语句、注释、未知函数、裸字段/纯常量。

调研依据见 docs/research/08-metric-semantic-layer-dsl.md：
主流语义层都不允许在指标表达式层写任意 SQL；分界线是"聚合之外还能不能有查询结构"。
"""
from __future__ import annotations

import pytest

from app.services.formula_guard import (
    ALLOWED_AGG_SQL_NAMES,
    ALLOWED_SCALAR_SQL_NAMES,
    FormulaRejected,
    is_formula_allowed,
    validate_formula,
)

# ── 允许清单（需求给定 + 常见派生写法）────────────────────────────────────

ALLOWED_FORMULAS = [
    'SUM("amount")',
    'AVG("amount")',
    'COUNT(DISTINCT "customer_id")',
    "COUNT(*)",
    "SUM(CASE WHEN \"status\"='paid' THEN \"amount\" ELSE 0 END)",
    "SUM(CASE WHEN \"status\"='paid' THEN \"amount\" END)",
    'SUM("amount")/NULLIF(COUNT(*),0)',
    'COALESCE(SUM("refund_amount"),0)',
    'metric("sales_amount")*1.2',
    'SUM({{amount}})',
    'ROUND(SUM("amount")/NULLIF(COUNT(*),0),2)',
    'CAST(SUM("amount") AS DECIMAL(18,2))',
    'COUNT_IF("amount">500)',
    # 常见派生写法（同一个白名单自然覆盖，顺手锁住）
    'AVG(price)',                                  # 裸字段在聚合内（既有测试用）
    "metric('B') * 2",                            # 单引号 key
    'SUM({{amount}}) / NULLIF(COUNT(*),0)',
    "SUM(CASE WHEN \"status\"='paid' AND \"qty\">0 THEN \"amount\" END)",
    "SUM(CASE WHEN \"status\" IS NULL THEN 1 ELSE 0 END)",
    'GREATEST(SUM("a"), 0)',
    'ABS(-SUM("a"))',
    'TRY_CAST(SUM("a") AS DOUBLE)',
    'SUM("a")%2',
    'MEDIAN("amount")',
    'STDDEV("amount")',
]


@pytest.mark.parametrize("formula", ALLOWED_FORMULAS)
def test_allowed_formulas_pass(formula: str):
    """白名单内的表达式必须全部放行（否则条件口径/安全除法又写不了）。"""
    validate_formula(formula)          # 不抛即通过
    assert is_formula_allowed(formula) is True


# ── 拒绝清单（需求给定）──────────────────────────────────────────────────

REJECTED_FORMULAS = [
    "(SELECT 1)",
    'SELECT * FROM "data"',
    'SUM("amount") FROM "data"',
    "1; DROP TABLE x",
    "READ_CSV('x.csv')",
    'evil_func(SUM("amount"))',
    'SUM("amount") OVER ()',
    "",
]


@pytest.mark.parametrize("formula", REJECTED_FORMULAS)
def test_rejected_formulas_raise_with_reason(formula: str):
    """禁用的结构必须被拒，且给出中文可读原因。"""
    with pytest.raises(FormulaRejected) as exc:
        validate_formula(formula)
    assert exc.value.reason.strip(), "拒绝原因不能为空"
    assert is_formula_allowed(formula) is False


# ── 拒绝原因要能定位（不是一句"非法"）────────────────────────────────────

@pytest.mark.parametrize(
    "formula,needle",
    [
        ("(SELECT 1)", "子查询"),
        ('SELECT * FROM "data"', ""),          # 解析失败或结构不支持，只要被拒即可
        ('SUM("amount") FROM "data"', "FROM"),
        ("1; DROP TABLE x", "多条语句"),
        ('evil_func(SUM("amount"))', "EVIL_FUNC"),
        ("READ_CSV('x.csv')", "READ_CSV"),
        ('SUM("amount") OVER ()', "窗口"),
        ("SUM(\"a\") -- c", "注释"),
        ("SUM(\"a\") /* c */", "注释"),
        ("SUM(\"a\") + 1 AS x", ""),           # 结构异常，只要被拒
    ],
)
def test_rejection_reason_is_specific(formula: str, needle: str):
    with pytest.raises(FormulaRejected) as exc:
        validate_formula(formula)
    if needle:
        assert needle.lower() in exc.value.reason.lower(), (
            f"拒绝原因应包含 {needle!r}，实际：{exc.value.reason}"
        )


# ── 结构性规则 ────────────────────────────────────────────────────────────

def test_window_is_explicitly_forbidden_with_future_hint():
    """窗口本轮禁止；注释里要指向未来由引擎生成的 share_of_total，而不是让人以为漏了。"""
    with pytest.raises(FormulaRejected) as exc:
        validate_formula('SUM("amount") OVER (PARTITION BY "region")')
    assert "窗口" in exc.value.reason
    assert "引擎" in exc.value.reason, "提示里应说明占比/累计由引擎生成"


def test_star_only_allowed_inside_count():
    validate_formula("COUNT(*)")                     # 允许
    with pytest.raises(FormulaRejected):
        validate_formula("SUM(*)")                   # 只允许 COUNT(*)
    with pytest.raises(FormulaRejected):
        validate_formula('COALESCE(*, 0)')


def test_distinct_only_allowed_inside_count():
    validate_formula('COUNT(DISTINCT "cid")')
    with pytest.raises(FormulaRejected):
        validate_formula('SUM(DISTINCT "amount")')


def test_bare_column_and_constant_rejected():
    """公式必须表达"一个聚合值"或"对其它指标的引用"，不能是裸字段/纯常量。"""
    for bad in ('amount', '"amount"', "amount * 2", "1", "1 + 1"):
        with pytest.raises(FormulaRejected):
            validate_formula(bad)


def test_column_outside_aggregate_rejected():
    """列必须写在聚合内部：SUM("a") + amount 里的 amount 是"没有聚合"的裸字段。"""
    with pytest.raises(FormulaRejected) as exc:
        validate_formula('SUM("a") + amount')
    assert "amount" in exc.value.reason


def test_qualified_column_rejected():
    """不支持带表前缀的列（没有 FROM，前缀无处绑定）。"""
    with pytest.raises(FormulaRejected):
        validate_formula('"data"."amount"')


def test_metric_reference_arg_validation():
    validate_formula('metric("sales_amount")')
    validate_formula("metric('sales_amount') * 2")
    for bad in ('metric()', 'metric("a", "b")', "metric(1)", "metric(sales_amount + 1)"):
        with pytest.raises(FormulaRejected):
            validate_formula(bad)


def test_placeholder_must_be_well_formed():
    validate_formula("SUM({{amount}})")
    validate_formula("SUM({{ amount }})")           # 允许占位符内空格
    for bad in ("SUM({{amount)}", "SUM({{1abc}})", "SUM({{amount}}) / {{"): 
        with pytest.raises(FormulaRejected):
            validate_formula(bad)


def test_placeholder_error_message_restores_original_formula():
    """错误信息/异常里要保留原始 {{field}}（哨兵标识符不外泄）。"""
    with pytest.raises(FormulaRejected) as exc:
        validate_formula("SUM({{amount}}) OVER ()")
    assert exc.value.formula == "SUM({{amount}}) OVER ()"
    assert "__lvco_ph_" not in exc.value.reason


def test_unknown_function_inside_valid_aggregate_rejected():
    with pytest.raises(FormulaRejected):
        validate_formula('SUM(evil_func("amount"))')


def test_underscore_delimited_extra_statements_rejected():
    for bad in ('SUM("a");SELECT 1', 'SUM("a");  SELECT 1'):
        with pytest.raises(FormulaRejected):
            validate_formula(bad)


# ── 白名单常量（供文档/外部引用，别悄悄改）────────────────────────────────

def test_whitelist_covers_required_aggregates_and_scalars():
    required_aggs = {
        "SUM", "AVG", "COUNT", "MIN", "MAX", "MEDIAN",
        "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
        "VARIANCE", "VARIANCE_POP", "VARIANCE_SAMP",
        "QUANTILE_CONT", "QUANTILE_DISC",
        "PERCENTILE_CONT", "PERCENTILE_DISC",
        "APPROX_QUANTILE", "APPROX_COUNT_DISTINCT",
        "ANY_VALUE", "COUNT_IF",
    }
    assert required_aggs <= ALLOWED_AGG_SQL_NAMES
    required_scalars = {
        "ROUND", "ABS", "CEIL", "CEILING", "FLOOR",
        "POWER", "SQRT", "LN", "LOG", "EXP",
        "GREATEST", "LEAST", "IFNULL", "MOD",
    }
    assert required_scalars <= ALLOWED_SCALAR_SQL_NAMES


# ── metric_service 接线：公开签名不变、旧危险输入仍拒、新表达式放行 ───────

def test_metric_service_accepts_new_expressions():
    from app.services.metric_service import assert_formula_allowed

    for good in (
        'SUM("amount")',
        "SUM(CASE WHEN \"status\"='paid' THEN \"amount\" ELSE 0 END)",
        'SUM("amount")/NULLIF(COUNT(*),0)',
        'COALESCE(SUM("refund_amount"),0)',
        'metric("sales_amount")*1.2',
        'SUM({{amount}})',
    ):
        assert_formula_allowed(good)


def test_metric_service_still_blocks_injection():
    from app.services.metric_service import MetricServiceError, assert_formula_allowed

    for bad in ("SUM(1);DROP TABLE users", "(SELECT 1)", 'SUM("a") OVER ()', "", "   "):
        with pytest.raises(MetricServiceError):
            assert_formula_allowed(bad)


def test_metric_service_error_message_is_the_agreed_copy():
    """对外文案统一（不含解析细节）；具体原因走日志与 err.reason。"""
    from app.services.metric_service import MetricServiceError, assert_formula_allowed

    with pytest.raises(MetricServiceError) as exc:
        assert_formula_allowed('SUM("a") OVER ()')
    assert str(exc.value) == (
        "指标 formula 结构不支持：只允许聚合、四则运算、CASE WHEN、COALESCE/NULLIF、"
        '类型转换与 metric("key") 引用；不支持子查询、窗口函数、多条语句。'
    )
    assert "窗口" in getattr(exc.value, "reason", "")


def test_metric_service_compat_names_still_work():
    """旧私有名保留为兼容包装（有外部 import 也不会断），且不再是第二份判定逻辑。"""
    from app.services import metric_service as ms

    assert ms._validate_derived_expression('SUM("a")') is True
    assert ms._validate_derived_expression("(SELECT 1)") is False
    assert set(ms._ALLOWED_AGGS) == set(ALLOWED_AGG_SQL_NAMES)


def test_resolve_to_expression_behaviour_unchanged():
    """模板指标 / 常量指标两条既有路径行为不变（resolve_to_expression 未改语义）。"""
    from app.services.metric_service import resolve_to_expression

    class _M:
        key = "sales_amount"
        formula = 'SUM({{amount}})'

    assert resolve_to_expression(_M(), ["amount"]) == 'SUM("amount")'

    class _C:
        key = "sales_amount"
        formula = 'SUM("amount")'

    assert resolve_to_expression(_C(), []) == 'SUM("amount")'
