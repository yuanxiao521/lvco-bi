"""指标语义层单元测试。

覆盖 P1（指标解析/校验/模板指标）与 P2（resolve_metric → 表达式度量 → query_engine 建 SQL）。
不依赖真实数据库 / DuckDB，纯逻辑校验。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.metric import MetricDefinition
from app.services.metric_service import (
    MetricServiceError,
    assert_formula_allowed,
    ensure_default_metrics,
    extract_metric_fields,
    get_metric,
    get_metric_by_key,
    resolve_metric,
    resolve_to_expression,
)
from app.services import query_engine as qe


def make_metric(**over) -> MetricDefinition:
    base = dict(
        id=uuid4(),
        key="sales_amount",
        name="销售额",
        formula="SUM(\"amount\")",
        agg_kind="SUM",
        user_id=None,
        active=True,
    )
    base.update(over)
    return MetricDefinition(**base)


# ── formula 校验 / 占位符提取 ─────────────────────────────────────────────

def test_assert_formula_allowed_ok_constants():
    assert_formula_allowed('SUM("amount")')
    assert_formula_allowed('COUNT(DISTINCT "customer_id")')
    assert_formula_allowed("AVG(price)")


def test_assert_formula_allowed_blocks_dangerous():
    with pytest.raises(MetricServiceError):
        assert_formula_allowed("SUM(1);DROP TABLE users")
    with pytest.raises(MetricServiceError):
        assert_formula_allowed("(SELECT 1)")


def test_extract_metric_fields():
    assert extract_metric_fields("SUM({{amount}})") == ["amount"]
    assert extract_metric_fields("SUM(\"amount\")") == []
    assert extract_metric_fields("COUNT(DISTINCT {{customer_id}})") == ["customer_id"]


# ── resolve_to_expression ─────────────────────────────────────────────────

def test_resolve_template_metric_with_dimensions():
    m = make_metric(formula="SUM({{amount}})")
    assert resolve_to_expression(m, ["amount"]) == 'SUM("amount")'


def test_resolve_template_metric_missing_field_raises():
    m = make_metric(formula="SUM({{amount}})")
    with pytest.raises(MetricServiceError):
        resolve_to_expression(m, [])


def test_resolve_constant_metric_returns_formula():
    m = make_metric(formula="SUM(\"amount\")")
    assert resolve_to_expression(m, []) == 'SUM("amount")'


# ── resolve_metric → 表达式度量 ───────────────────────────────────────────

def test_resolve_metric_builds_expression_measure():
    m = make_metric()
    cfg = resolve_metric(m, dimensions=["region"])
    assert cfg.dimensions == ["region"]
    measure = cfg.measures[0]
    assert measure.expression == 'SUM("amount")'  # 无占位符 → 原样
    assert measure.alias == "sum_sales_amount"


def test_resolve_metric_template_builds_expression():
    m = make_metric(formula="SUM({{amount}})")
    cfg = resolve_metric(m, dimensions=["amount"])
    assert cfg.measures[0].expression == 'SUM("amount")'


# ── query_engine /_build_select 支持表达式度量 ────────────────────────────

def test_build_select_expression():
    dims, cols = qe._build_select(
        ["region"],
        [{"field": "", "agg": "SUM", "expression": 'SUM("amount")', "alias": "sum_sales_amount"}],
    )
    assert '"region"' in dims
    assert 'SUM("amount") AS "sum_sales_amount"' in dims
    assert cols == ["region", "sum_sales_amount"]


def test_build_select_expression_without_alias():
    dims, cols = qe._build_select(
        ["region"],
        [{"field": "", "agg": "SUM", "expression": "SUM(amount)"}],
    )
    assert 'AS "sum_amount"' in dims
    assert cols == ["region", "sum_amount"]


# ── CRUD / 检索（隔离 DB）────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_metric_by_key_prefers_private():
    private = make_metric(key="sales_amount", user_id=uuid4())
    global_m = make_metric(key="sales_amount", user_id=None)
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=private)))
    got = await get_metric_by_key(db, "sales_amount", private.user_id)
    assert got is private


@pytest.mark.asyncio
async def test_get_metric_not_found_raises():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    with pytest.raises(MetricServiceError):
        await get_metric(db, uuid4(), None)


@pytest.mark.asyncio
async def test_ensure_default_metrics_inserts_when_missing():
    db = MagicMock()
    # 所有模板 key 在库中均不存在 → scalar_one_or_none 返回 None（触发插入）
    db.execute = AsyncMock(side_effect=lambda *a, **k: MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    db.add = MagicMock()
    db.flush = AsyncMock()
    seed_count = len(ensure_default_metrics.__globals__["DEFAULT_TEMPLATE_METRICS"])
    await ensure_default_metrics(db)
    assert db.add.call_count == seed_count


# ── P5: 指标清单注入 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_metrics_for_user_filters_active():
    m1 = make_metric(key="sales_amount", name="销售额")
    m2 = make_metric(key="order_count", name="订单量")
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=[m1, m2]))
    )))
    from app.services.metric_service import list_metrics_for_user

    got = await list_metrics_for_user(db, uuid4())
    assert got == [m1, m2]


def test_format_metrics_context_lists_keys_and_formulas():
    from app.services.metric_service import format_metrics_context

    m = make_metric(key="sales_amount", name="销售额",
                    description="成交金额合计", formula="SUM({{amount}})")
    ctx = format_metrics_context([m])
    assert "sales_amount" in ctx
    assert "销售额" in ctx
    assert "SUM({{amount}})" in ctx
    assert "可引用指标清单" in ctx


def test_format_metrics_context_empty():
    from app.services.metric_service import format_metrics_context

    assert format_metrics_context([]) == ""


@pytest.mark.asyncio
async def test_resolve_measures_metric_fill_field_takes_precedence():
    """模板指标（SUM({{amount}})）应优先用度量自带的 field 填充占位，
    而不是误用维度分组字段。"""
    from app.services.metric_service import resolve_measures
    from app.models.metric import MetricDefinition

    met = make_metric(key="sales_amount", formula="SUM({{amount}})")
    # 构造一个能按 key 查到的 mock DB（scalar_one_or_none 返回指标）
    class _FakeResult:
        def __init__(self, metric): self._metric = metric
        def scalar_one_or_none(self): return self._metric

    db = MagicMock()
    db.execute = AsyncMock(side_effect=lambda *a, **k: _FakeResult(met))

    exe, disp = await resolve_measures(db, None, [{"metric_key": "sales_amount", "field": "amount"}], dimensions=["region"])
    assert exe[0].expression == 'SUM("amount")'
    assert disp[0]["metric_key"] == "sales_amount"
    assert "metric_id" in disp[0]