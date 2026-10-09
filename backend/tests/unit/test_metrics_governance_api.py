from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from app.api.v1 import metrics as metrics_api
from app.models.metric import MetricDefinition
from app.models.metric_version import MetricVersion
from app.services.metric_service import MetricServiceError

MID = uuid4()
USER_ID = uuid4()


def make_metric(**over) -> MetricDefinition:
    base = dict(
        id=MID,
        key="sales_amount",
        name="销售额",
        formula="SUM(\"amount\")",
        formula_type="basic",
        version=1,
        agg_kind="SUM",
        user_id=None,
        active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return MetricDefinition(**base)


def _metric_present(db, present=True):
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=object() if present else None)))


def _mock_service():
    return patch("app.api.v1.metrics.MetricService")


async def test_dependencies_success():
    db = MagicMock()
    dep = make_metric(id=uuid4(), name="依赖指标", key="dep")
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=object()),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[dep]))),
        )
    )
    with _mock_service() as MockService:
        MockService.return_value.resolve_dependencies = AsyncMock(return_value=[str(dep.id)])
        resp = await metrics_api.get_metric_dependencies(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert len(resp.data) == 1
    assert resp.data[0]["name"] == "依赖指标"
    assert resp.data[0]["formulaType"] == "basic"


async def test_dependencies_metric_not_found_404():
    db = MagicMock()
    _metric_present(db, present=False)
    with pytest.raises(HTTPException) as exc:
        await metrics_api.get_metric_dependencies(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_dependencies_empty_deps():
    db = MagicMock()
    _metric_present(db)
    with _mock_service() as MockService:
        MockService.return_value.resolve_dependencies = AsyncMock(return_value=[])
        resp = await metrics_api.get_metric_dependencies(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data == []


async def test_dependents_success():
    db = MagicMock()
    dep = make_metric(id=uuid4(), name="下游指标", key="downstream", formula_type="derived", depends_on_metric_ids=[str(MID)])
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=object()),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[dep]))),
        )
    )
    resp = await metrics_api.get_metric_dependents(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data[0]["name"] == "下游指标"
    assert resp.data[0]["formulaType"] == "derived"
    assert resp.data[0]["dependsOnMetricIds"] == [str(MID)]


async def test_dependents_metric_not_found_404():
    db = MagicMock()
    _metric_present(db, present=False)
    with pytest.raises(HTTPException) as exc:
        await metrics_api.get_metric_dependents(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_dependents_empty():
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=object()),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )
    resp = await metrics_api.get_metric_dependents(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data == []


async def test_impact_success():
    db = MagicMock()
    report = {"metric_id": str(MID), "name": "销售额", "dependents_count": 3, "dashboards_count": 2, "users_count": 1}
    with _mock_service() as MockService:
        MockService.return_value.impact_analysis = AsyncMock(return_value=report)
        resp = await metrics_api.get_metric_impact(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data["dependents_count"] == 3


async def test_impact_service_error_404():
    db = MagicMock()
    with _mock_service() as MockService:
        MockService.return_value.impact_analysis = AsyncMock(side_effect=MetricServiceError("指标不存在"))
        with pytest.raises(HTTPException) as exc:
            await metrics_api.get_metric_impact(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_lineage_success():
    db = MagicMock()
    _metric_present(db)
    lineage = [{"source_field": "amount", "transform": "SUM", "target_field": "amount", "dataset_name": "ds"}]
    with _mock_service() as MockService:
        MockService.return_value.get_lineage = AsyncMock(return_value=lineage)
        resp = await metrics_api.get_metric_lineage(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data[0]["transform"] == "SUM"


async def test_lineage_metric_not_found_404():
    db = MagicMock()
    _metric_present(db, present=False)
    with pytest.raises(HTTPException) as exc:
        await metrics_api.get_metric_lineage(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_publish_version_success_201():
    db = MagicMock()
    body = metrics_api._PublishVersionRequest(change_note="新增口径")
    ver = MagicMock(id=uuid4(), metric_id=MID, version=4, change_note="新增口径", created_by=USER_ID, created_at=datetime.now(timezone.utc))
    with _mock_service() as MockService:
        MockService.return_value.publish_version = AsyncMock(return_value=ver)
        resp = await metrics_api.publish_metric_version(MID, body, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data["version"] == 4
    assert resp.data["change_note"] == "新增口径"


async def test_publish_version_service_error_404():
    db = MagicMock()
    body = metrics_api._PublishVersionRequest(change_note="note")
    with _mock_service() as MockService:
        MockService.return_value.publish_version = AsyncMock(side_effect=MetricServiceError("指标不存在"))
        with pytest.raises(HTTPException) as exc:
            await metrics_api.publish_metric_version(MID, body, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_rollback_success():
    db = MagicMock()
    body = metrics_api._RollbackRequest(version=2)
    metric = make_metric()
    with _mock_service() as MockService:
        MockService.return_value.rollback_to = AsyncMock(return_value=metric)
        resp = await metrics_api.rollback_metric(MID, body, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data["key"] == "sales_amount"
    assert resp.data["name"] == "销售额"


async def test_rollback_service_error_404():
    db = MagicMock()
    body = metrics_api._RollbackRequest(version=99)
    with _mock_service() as MockService:
        MockService.return_value.rollback_to = AsyncMock(side_effect=MetricServiceError("版本记录不存在"))
        with pytest.raises(HTTPException) as exc:
            await metrics_api.rollback_metric(MID, body, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_compare_versions_success():
    db = MagicMock()
    diff = {"metric_id": str(MID), "from_version": 1, "to_version": 2, "changed": True, "diff_fields": [{"field": "formula", "from": "a", "to": "b"}]}
    with _mock_service() as MockService:
        MockService.return_value.compare_versions = AsyncMock(return_value=diff)
        resp = await metrics_api.compare_metric_versions(MID, 1, 2, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data["changed"] is True
    assert resp.data["diff_fields"][0]["field"] == "formula"


async def test_compare_versions_service_error_404():
    db = MagicMock()
    with _mock_service() as MockService:
        MockService.return_value.compare_versions = AsyncMock(side_effect=MetricServiceError("版本不存在"))
        with pytest.raises(HTTPException) as exc:
            await metrics_api.compare_metric_versions(MID, 1, 99, current_user=MagicMock(id=USER_ID), db=db)
    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_dependents_single_object_serialization():
    # 血缘图下游节点：返回 MetricDefinition 的 camelCase 序列化
    db = MagicMock()
    dep = make_metric(
        id=uuid4(),
        name="下游",
        key="d",
        formula='metric("x") * 2',
        formula_type="derived",
        depends_on_metric_ids=[str(MID)],
        agg_kind="SUM",
        table_ref="t",
    )
    db.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=object()),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[dep]))),
        )
    )
    resp = await metrics_api.get_metric_dependents(MID, current_user=MagicMock(id=USER_ID), db=db)
    item = resp.data[0]
    assert item["name"] == "下游"
    assert item["formulaType"] == "derived"
    assert item["dependsOnMetricIds"] == [str(MID)]
    assert item["aggKind"] == "SUM"
    assert item["tableRef"] == "t"


async def test_list_metric_versions_empty():
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    )
    resp = await metrics_api.list_metric_versions(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert resp.data == []


async def test_list_metric_versions_sorted():
    db = MagicMock()
    now = datetime.now(timezone.utc)
    v1 = MagicMock(version=1, change_note="基线", created_at=now)
    v2 = MagicMock(version=2, change_note="新增口径", created_at=now)
    db.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[v1, v2]))))
    )
    resp = await metrics_api.list_metric_versions(MID, current_user=MagicMock(id=USER_ID), db=db)
    assert [item["version"] for item in resp.data] == [1, 2]
    assert resp.data[1]["change_note"] == "新增口径"


async def test_infer_metric_meta_derived():
    db = MagicMock()
    dep_id = uuid4()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[(dep_id, "b")])))
    formula_type, dep_ids = await metrics_api._infer_metric_meta(db, USER_ID, "metric('b') * 2")
    assert formula_type == "derived"
    assert dep_ids == [str(dep_id)]


async def test_infer_metric_meta_derived_missing_dep_keeps_derived():
    # 公式引用了尚未创建的指标 key：仍标记派生，但不回填不存在的依赖 ID
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    formula_type, dep_ids = await metrics_api._infer_metric_meta(db, USER_ID, "metric('other') * 2")
    assert formula_type == "derived"
    assert dep_ids == []


async def test_infer_metric_meta_basic():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    formula_type, dep_ids = await metrics_api._infer_metric_meta(db, USER_ID, 'SUM("amount")')
    assert formula_type == "basic"
    assert dep_ids == []


async def test_infer_metric_meta_invalid_formula_falls_back_to_basic():
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    formula_type, dep_ids = await metrics_api._infer_metric_meta(db, USER_ID, "metric(")
    assert formula_type == "basic"
    assert dep_ids == []