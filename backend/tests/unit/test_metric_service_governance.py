from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.metric import MetricDefinition
from app.models.metric_usage import MetricUsage
from app.services.metric_service import MetricService, MetricServiceError


def make_metric(**over) -> MetricDefinition:
    base = dict(
        id=uuid4(),
        key="sales_amount",
        name="销售额",
        formula="SUM(\"amount\")",
        agg_kind="SUM",
        user_id=None,
        active=True,
        version=1,
        formula_type="basic",
        depends_on_metric_ids=None,
    )
    base.update(over)
    return MetricDefinition(**base)


def fake_result(scalar):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar)
    return r


def fake_scalars(items):
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=items)))
    return r


def make_uow(execute_side_effect=None):
    uow = MagicMock()
    uow.flush = AsyncMock()
    if execute_side_effect is not None:
        uow.db.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        uow.db.execute = AsyncMock()
    return uow


def make_service(uow):
    return MetricService(uow)


def make_usage(usage_type="dashboard", usage_ref_id="d1", user_id=None) -> MetricUsage:
    return MetricUsage(
        metric_id=uuid4(),
        usage_type=usage_type,
        usage_ref_id=usage_ref_id,
        user_id=user_id or uuid4(),
    )


# ── resolve_dependencies ────────────────────────────────────────────────────

async def test_resolve_dependencies_empty_when_no_deps():
    m = make_metric(id=uuid4(), depends_on_metric_ids=[])
    uow = make_uow([fake_result(m)])
    svc = make_service(uow)
    result = await svc.resolve_dependencies(m.id)
    assert result == []


async def test_resolve_dependencies_single_level():
    mid = uuid4()
    m = make_metric(id=mid, depends_on_metric_ids=["d1", "d2"])
    uow = make_uow([fake_result(m), fake_result(None), fake_result(None)])
    svc = make_service(uow)
    result = await svc.resolve_dependencies(mid)
    assert result == ["d1", "d2"]


async def test_resolve_dependencies_multi_level_bfs():
    mid = uuid4()
    m = make_metric(id=mid, depends_on_metric_ids=["b", "c"])
    b = make_metric(id="b", depends_on_metric_ids=["d"])
    c = make_metric(id="c", depends_on_metric_ids=[])
    d = make_metric(id="d", depends_on_metric_ids=[])
    uow = make_uow([fake_result(m), fake_result(b), fake_result(c), fake_result(d)])
    svc = make_service(uow)
    result = await svc.resolve_dependencies(mid)
    assert result == ["b", "c", "d"]


async def test_resolve_dependencies_skips_missing_metric():
    mid = uuid4()
    m = make_metric(id=mid, depends_on_metric_ids=["ghost"])
    uow = make_uow([fake_result(m), fake_result(None)])
    svc = make_service(uow)
    result = await svc.resolve_dependencies(mid)
    assert result == ["ghost"]


async def test_resolve_dependencies_dedup_diamond():
    mid = uuid4()
    m = make_metric(id=mid, depends_on_metric_ids=["a", "b"])
    a = make_metric(id="a", depends_on_metric_ids=["x"])
    b = make_metric(id="b", depends_on_metric_ids=["x"])
    x = make_metric(id="x", depends_on_metric_ids=[])
    uow = make_uow([fake_result(m), fake_result(a), fake_result(b), fake_result(x)])
    svc = make_service(uow)
    result = await svc.resolve_dependencies(mid)
    assert result == ["a", "b", "x"]


async def test_resolve_dependencies_cycle_terminates():
    mid = uuid4()
    m = make_metric(id=mid, depends_on_metric_ids=["n1"])
    n1 = make_metric(id="n1", depends_on_metric_ids=[str(mid)])
    uow = make_uow([fake_result(m), fake_result(n1), fake_result(None)])
    svc = make_service(uow)
    result = await svc.resolve_dependencies(mid)
    assert result == ["n1", str(mid)]


# ── get_dependents ─────────────────────────────────────────────────────────

async def test_get_dependents_returns_list():
    items = [make_usage(), make_usage()]
    uow = make_uow([fake_scalars(items)])
    svc = make_service(uow)
    result = await svc.get_dependents(uuid4())
    assert result == items


async def test_get_dependents_empty():
    uow = make_uow([fake_scalars([])])
    svc = make_service(uow)
    result = await svc.get_dependents(uuid4())
    assert result == []


async def test_get_dependents_excludes_deleted():
    live = make_usage()
    deleted = make_usage()
    deleted.deleted_at_dummy = True
    uow = make_uow([fake_scalars([live, deleted])])
    svc = make_service(uow)
    result = await svc.get_dependents(uuid4())
    assert result == [live, deleted]


async def test_get_dependents_returns_usage_objects():
    item = make_usage(usage_type="canvas", usage_ref_id="c_1")
    uow = make_uow([fake_scalars([item])])
    svc = make_service(uow)
    result = await svc.get_dependents(uuid4())
    assert result[0].usage_type == "canvas"
    assert result[0].usage_ref_id == "c_1"


# ── get_lineage ────────────────────────────────────────────────────────────

async def test_get_lineage_returns_dicts():
    lineage = MagicMock()
    lineage.source_field = "amount"
    lineage.transform = "SUM"
    lineage.target_field = "amount"
    row = (lineage, "销售数据源")
    uow = make_uow([MagicMock(all=MagicMock(return_value=[row]))])
    svc = make_service(uow)
    result = await svc.get_lineage(uuid4())
    assert result == [{
        "source_field": "amount",
        "transform": "SUM",
        "target_field": "amount",
        "dataset_name": "销售数据源",
    }]


async def test_get_lineage_empty():
    uow = make_uow([MagicMock(all=MagicMock(return_value=[]))])
    svc = make_service(uow)
    result = await svc.get_lineage(uuid4())
    assert result == []


async def test_get_lineage_multiple_rows():
    lineage1 = MagicMock(source_field="amount", transform="SUM", target_field="a")
    lineage2 = MagicMock(source_field="order_id", transform="COUNT", target_field="o")
    uow = make_uow([MagicMock(all=MagicMock(return_value=[(lineage1, "ds"), (lineage2, "ds")]))])
    svc = make_service(uow)
    result = await svc.get_lineage(uuid4())
    assert len(result) == 2
    assert result[1]["transform"] == "COUNT"


async def test_get_lineage_none_dataset_name():
    lineage = MagicMock(source_field="amount", transform="SUM", target_field="a")
    uow = make_uow([MagicMock(all=MagicMock(return_value=[(lineage, None)]))])
    svc = make_service(uow)
    result = await svc.get_lineage(uuid4())
    assert result[0]["dataset_name"] is None


# ── impact_analysis ────────────────────────────────────────────────────────

async def test_impact_analysis_basic_counts():
    mid = uuid4()
    m = make_metric(id=mid, key="sales_amount", name="销售额")
    deps = [
        make_usage(usage_type="dashboard", usage_ref_id="d1"),
        make_usage(usage_type="canvas", usage_ref_id="c1"),
        make_usage(usage_type="ai_query", usage_ref_id=None),
    ]
    uow = make_uow([fake_result(m), fake_scalars(deps), fake_scalars([])])
    svc = make_service(uow)
    result = await svc.impact_analysis(mid)
    assert result["metric_id"] == str(mid)
    assert result["name"] == "销售额"
    assert result["dependents_count"] == 3
    assert result["dashboards_count"] == 1


async def test_impact_analysis_counts_distinct_users():
    mid = uuid4()
    m = make_metric(id=mid, key="k", name="n")
    user = uuid4()
    deps = [
        make_usage(usage_type="dashboard", usage_ref_id="d1", user_id=user),
        make_usage(usage_type="dashboard", usage_ref_id="d2", user_id=user),
        make_usage(usage_type="canvas", usage_ref_id="c1", user_id=uuid4()),
    ]
    uow = make_uow([fake_result(m), fake_scalars(deps), fake_scalars([])])
    svc = make_service(uow)
    result = await svc.impact_analysis(mid)
    assert result["users_count"] == 2
    assert result["dependents_count"] == 3


async def test_impact_analysis_zero_dependents():
    mid = uuid4()
    m = make_metric(id=mid, key="k", name="n")
    uow = make_uow([fake_result(m), fake_scalars([]), fake_scalars([])])
    svc = make_service(uow)
    result = await svc.impact_analysis(mid)
    assert result["dependents_count"] == 0
    assert result["dashboards_count"] == 0
    assert result["users_count"] == 0


async def test_impact_analysis_metric_not_found():
    uow = make_uow([fake_result(None)])
    svc = make_service(uow)
    with pytest.raises(MetricServiceError):
        await svc.impact_analysis(uuid4())


async def test_impact_analysis_counts_distinct_dashboards():
    mid = uuid4()
    m = make_metric(id=mid, key="k", name="n")
    deps = [
        make_usage(usage_type="dashboard", usage_ref_id="d1"),
        make_usage(usage_type="dashboard", usage_ref_id="d1"),
        make_usage(usage_type="dashboard", usage_ref_id="d2"),
    ]
    uow = make_uow([fake_result(m), fake_scalars(deps), fake_scalars([])])
    svc = make_service(uow)
    result = await svc.impact_analysis(mid)
    assert result["dashboards_count"] == 2


async def test_impact_analysis_ignores_non_dashboard_usage():
    mid = uuid4()
    m = make_metric(id=mid, key="k", name="n")
    deps = [
        make_usage(usage_type="canvas", usage_ref_id="c1"),
        make_usage(usage_type="ai_query", usage_ref_id=None),
    ]
    uow = make_uow([fake_result(m), fake_scalars(deps), fake_scalars([])])
    svc = make_service(uow)
    result = await svc.impact_analysis(mid)
    assert result["dashboards_count"] == 0