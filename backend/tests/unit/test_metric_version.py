from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.metric import MetricDefinition
from app.models.metric_version import MetricVersion
from app.services.metric_service import MetricService, MetricServiceError


def make_metric(**over) -> MetricDefinition:
    base = dict(
        id=uuid4(),
        key="sales_amount",
        name="销售额",
        description="口径说明",
        formula="SUM(\"amount\")",
        agg_kind="SUM",
        user_id=None,
        active=True,
        version=3,
        formula_type="basic",
        depends_on_metric_ids=None,
        datasource_id=None,
        table_ref="data",
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
    uow.db.add = MagicMock()
    uow.db.refresh = AsyncMock()
    if execute_side_effect is not None:
        uow.db.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        uow.db.execute = AsyncMock()
    return uow


def make_snapshot(**over) -> dict:
    base = {
        "key": "sales_amount",
        "name": "销售额",
        "description": "旧口径",
        "formula": "SUM(\"amount\")",
        "agg_kind": "SUM",
        "datasource_id": None,
        "table_ref": "data",
        "active": True,
        "formula_type": "basic",
        "depends_on_metric_ids": [],
    }
    base.update(over)
    return base


# ── publish_version ────────────────────────────────────────────────────────

async def test_publish_version_increments_version():
    mid = uuid4()
    m = make_metric(id=mid, version=3)
    uow = make_uow([fake_result(m)])
    svc = MetricService(uow)
    version = await svc.publish_version(mid, "新增口径", uuid4())
    assert version.version == 4
    assert version.metric_id == mid
    assert version.change_note == "新增口径"
    assert m.version == 4
    assert uow.db.add.called


async def test_publish_version_snapshot_contains_fields():
    mid = uuid4()
    m = make_metric(id=mid, key="revenue", name="营收", formula="SUM(\"revenue\")", agg_kind="SUM", version=1)
    uow = make_uow([fake_result(m)])
    svc = MetricService(uow)
    version = await svc.publish_version(mid, "first", uuid4())
    assert version.snapshot["key"] == "revenue"
    assert version.snapshot["formula"] == "SUM(\"revenue\")"
    assert version.snapshot["agg_kind"] == "SUM"


async def test_publish_version_metric_not_found():
    uow = make_uow([fake_result(None)])
    svc = MetricService(uow)
    with pytest.raises(MetricServiceError):
        await svc.publish_version(uuid4(), "note", uuid4())


async def test_publish_version_preserves_derived_deps():
    mid = uuid4()
    m = make_metric(id=mid, formula_type="derived", depends_on_metric_ids=["a", "b"], version=2)
    uow = make_uow([fake_result(m)])
    svc = MetricService(uow)
    version = await svc.publish_version(mid, "derived change", uuid4())
    assert version.version == 3
    assert version.snapshot["formula_type"] == "derived"
    assert version.snapshot["depends_on_metric_ids"] == ["a", "b"]


# ── rollback_to ────────────────────────────────────────────────────────────

async def test_rollback_to_restores_snapshot_and_bumps_version():
    mid = uuid4()
    ver = MetricVersion(metric_id=mid, version=2, snapshot=make_snapshot(formula="SUM(\"old\")"), change_note="v2", created_by=uuid4())
    m = make_metric(id=mid, formula='SUM("new")', version=5)
    uow = make_uow([fake_result(ver), fake_result(m)])
    svc = MetricService(uow)
    restored = await svc.rollback_to(mid, 2, uuid4())
    assert restored.version == 6
    assert restored.formula == 'SUM("old")'
    assert uow.db.add.called


async def test_rollback_to_restores_key_and_name():
    mid = uuid4()
    ver = MetricVersion(metric_id=mid, version=1, snapshot=make_snapshot(key="old_key", name="旧指标"), change_note="v1", created_by=uuid4())
    m = make_metric(id=mid)
    uow = make_uow([fake_result(ver), fake_result(m)])
    svc = MetricService(uow)
    restored = await svc.rollback_to(mid, 1, uuid4())
    assert restored.key == "old_key"
    assert restored.name == "旧指标"


async def test_rollback_to_version_not_found():
    mid = uuid4()
    uow = make_uow([fake_result(None)])
    svc = MetricService(uow)
    with pytest.raises(MetricServiceError):
        await svc.rollback_to(mid, 99, uuid4())


async def test_rollback_to_metric_not_found():
    mid = uuid4()
    ver = MetricVersion(metric_id=mid, version=1, snapshot=make_snapshot(), change_note="v1", created_by=uuid4())
    uow = make_uow([fake_result(ver), fake_result(None)])
    svc = MetricService(uow)
    with pytest.raises(MetricServiceError):
        await svc.rollback_to(mid, 1, uuid4())


# ── compare_versions ───────────────────────────────────────────────────────

async def test_compare_versions_detects_changes():
    mid = uuid4()
    v1 = MetricVersion(metric_id=mid, version=1, snapshot=make_snapshot(formula="SUM(\"a\")", agg_kind="SUM"), change_note="v1")
    v2 = MetricVersion(metric_id=mid, version=2, snapshot=make_snapshot(formula="COUNT(\"b\")", agg_kind="COUNT"), change_note="v2")
    uow = make_uow([fake_scalars([v1, v2])])
    svc = MetricService(uow)
    diff = await svc.compare_versions(mid, 1, 2)
    assert diff["changed"] is True
    fields = {d["field"] for d in diff["diff_fields"]}
    assert "formula" in fields
    assert "agg_kind" in fields


async def test_compare_versions_no_diff():
    mid = uuid4()
    snap = make_snapshot(formula="SUM(\"a\")")
    v1 = MetricVersion(metric_id=mid, version=1, snapshot=snap, change_note="v1")
    v2 = MetricVersion(metric_id=mid, version=2, snapshot=dict(snap), change_note="v2")
    uow = make_uow([fake_scalars([v1, v2])])
    svc = MetricService(uow)
    diff = await svc.compare_versions(mid, 1, 2)
    assert diff["changed"] is False
    assert diff["diff_fields"] == []


async def test_compare_versions_missing_from_raises():
    mid = uuid4()
    v2 = MetricVersion(metric_id=mid, version=2, snapshot=make_snapshot(), change_note="v2")
    uow = make_uow([fake_scalars([v2])])
    svc = MetricService(uow)
    with pytest.raises(MetricServiceError):
        await svc.compare_versions(mid, 1, 2)


async def test_compare_versions_missing_to_raises():
    mid = uuid4()
    v1 = MetricVersion(metric_id=mid, version=1, snapshot=make_snapshot(), change_note="v1")
    uow = make_uow([fake_scalars([v1])])
    svc = MetricService(uow)
    with pytest.raises(MetricServiceError):
        await svc.compare_versions(mid, 1, 2)