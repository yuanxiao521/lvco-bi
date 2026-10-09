from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.metric import MetricDefinition
from app.schemas.query import QueryResult
from app.services import query_engine as qe
from app.services.query_engine import CircularDependencyError


def make_metric(**over) -> MetricDefinition:
    base = dict(
        id="AAAA-BBBB",
        key="derived_a",
        name="派生A",
        formula="metric('B') * 2",
        agg_kind="SUM",
        formula_type="derived",
        depends_on_metric_ids=[],
    )
    base.update(over)
    return MetricDefinition(**base)


def make_result():
    return QueryResult(columns=["v"], rows=[{"v": 1}], query_time_ms=5)


def make_context(db=None, **over) -> dict:
    ctx = {
        "db": db or MagicMock(),
        "user_id": "uid",
        "datasource_id": "dsid",
        "dimensions": ["region"],
        "filters": [],
        "chart_type": "bar",
        "limit": 1000,
    }
    ctx.update(over)
    return ctx


async def test_derived_no_deps_executes_and_caches():
    m = make_metric(id="AAAA-BBBB", formula_type="derived", depends_on_metric_ids=[])
    result = make_result()
    ctx = make_context()
    with patch("app.services.query_engine.execute_chart_query", new=AsyncMock(return_value=result)) as mock_exec:
        out = await qe.execute_derived_metric("AAAA-BBBB", m, ctx)
    assert out is result
    assert ctx["_cache"]["AAAA-BBBB"] is result
    mock_exec.assert_awaited_once()


async def test_derived_recursive_two_levels():
    a = make_metric(id="A", key="a", formula_type="derived", depends_on_metric_ids=["B"], formula="metric('B') * 2")
    b = make_metric(id="B", key="b", formula_type="derived", depends_on_metric_ids=[], formula="SUM(\"amount\")")
    result_b = make_result()
    result_a = make_result()
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=b)))
    ctx = make_context(db=db)
    with patch("app.services.query_engine.execute_chart_query", new=AsyncMock(side_effect=[result_b, result_a])) as mock_exec:
        out = await qe.execute_derived_metric("A", a, ctx)
    assert out is result_a
    assert mock_exec.await_count == 2
    assert ctx["_cache"]["A"] is result_a
    assert ctx["_cache"]["B"] is result_b


async def test_derived_cache_hit_skips_execution():
    m = make_metric(id="A", formula_type="derived", depends_on_metric_ids=[])
    cached = make_result()
    ctx = make_context(_cache={"A": cached})
    with patch("app.services.query_engine.execute_chart_query", new=AsyncMock()) as mock_exec:
        out = await qe.execute_derived_metric("A", m, ctx)
    assert out is cached
    mock_exec.assert_not_awaited()


async def test_derived_circular_dependency_raises():
    a = make_metric(id="A", formula_type="derived", depends_on_metric_ids=["A"], formula="metric('A') * 2")
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=a)))
    ctx = make_context(db=db)
    with patch("app.services.query_engine.execute_chart_query", new=AsyncMock()) as mock_exec:
        with pytest.raises(CircularDependencyError):
            await qe.execute_derived_metric("A", a, ctx)
    mock_exec.assert_not_awaited()


async def test_derived_dep_cycle_between_two_metrics():
    a = make_metric(id="A", formula_type="derived", depends_on_metric_ids=["B"])
    b = make_metric(id="B", formula_type="derived", depends_on_metric_ids=["A"])
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        MagicMock(scalar_one_or_none=MagicMock(return_value=b)),
        MagicMock(scalar_one_or_none=MagicMock(return_value=a)),
    ])
    ctx = make_context(db=db)
    with patch("app.services.query_engine.execute_chart_query", new=AsyncMock()) as mock_exec:
        with pytest.raises(CircularDependencyError):
            await qe.execute_derived_metric("A", a, ctx)
    mock_exec.assert_not_awaited()


async def test_derived_stack_cleaned_after_run():
    a = make_metric(id="A", formula_type="derived", depends_on_metric_ids=[])
    db = MagicMock()
    db.execute = AsyncMock()
    ctx = make_context(db=db)
    result = make_result()
    with patch("app.services.query_engine.execute_chart_query", new=AsyncMock(return_value=result)):
        await qe.execute_derived_metric("A", a, ctx)
    assert "A" not in ctx["_recursion_stack"]