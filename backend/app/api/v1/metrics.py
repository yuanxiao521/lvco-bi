import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.metric import MetricDefinition
from app.models.metric_usage import MetricUsage
from app.models.metric_version import MetricVersion
from app.models.user import User
from app.schemas import (
    MetricCreate,
    MetricResponse,
    MetricUpdate,
    SuccessResponse,
)
from app.repositories.unit_of_work import UnitOfWork
from app.services.metric_parser import parse_derived_formula
from app.services.metric_service import MetricService, MetricServiceError, assert_formula_allowed

router = APIRouter(prefix="/metrics", tags=["指标中心"])

logger = logging.getLogger("lvco.metrics")


async def _infer_metric_meta(
    db: AsyncSession, user_id: UUID, formula: str
) -> tuple[str, list[str]]:
    """依据公式中的 metric('key') 引用推断公式类型与依赖指标 ID。

    Returns:
        (formula_type, depends_on_metric_ids)：派生公式返回 ("derived", [ids])，
        否则 ("basic", [])。解析失败时安全退化为基础指标，不阻断创建/更新。
    """
    try:
        refs = parse_derived_formula(formula)
    except ValueError:
        return "basic", []
    keys = list({r.metric_key for r in refs if r.metric_key})
    dep_ids: list[str] = []
    if keys:
        dep_rows = await db.execute(
            select(MetricDefinition.id, MetricDefinition.key).where(
                MetricDefinition.key.in_(keys),
                MetricDefinition.user_id == user_id,
            )
        )
        by_key = {key: str(mid) for mid, key in dep_rows.all()}
        dep_ids = [by_key[k] for k in keys if k in by_key]
    return ("derived" if refs else "basic"), dep_ids

# 后台联动刷新任务引用（防止请求结束后被 GC）
_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def _trigger_dependent_dashboards(metric_id: UUID, db: AsyncSession) -> None:
    """指标口径变更后，异步刷新所有引用该指标的仪表盘并 SSE 通知所有者。

    通过 MetricUsage(usage_type='dashboard') 定位受影响仪表盘，后台刷新 + 推送
    ``dashboard_updated``，不阻塞本次指标更新的 HTTP 响应。
    """
    try:
        rows = (
            await db.execute(
                select(MetricUsage.usage_ref_id).where(
                    MetricUsage.metric_id == metric_id,
                    MetricUsage.usage_type == "dashboard",
                    MetricUsage.usage_ref_id.is_not(None),
                    MetricUsage.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        dashboard_ids = {UUID(str(r)) for r in rows if r}
    except Exception:
        # 联动刷新是附加能力，定位失败时不阻断指标更新
        logger.warning("metric_dashboard_refresh_lookup_skipped", exc_info=True)
        return
    if not dashboard_ids:
        return

    from app.services.dashboard_scheduler import refresh_dashboard_and_notify

    for did in dashboard_ids:
        task = asyncio.create_task(refresh_dashboard_and_notify(did))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)


def _visible_scope(user_id: UUID):
    """指标可见范围：用户私有 + 全局/公开模板。"""
    return (MetricDefinition.user_id == user_id) | (MetricDefinition.user_id.is_(None))


def _metric_dict(m: MetricDefinition) -> dict:
    """把指标 ORM 序列化为前端 MetricDefinition 需要的 camelCase 字典（血缘图/下游依赖用）。"""
    return {
        "id": str(m.id),
        "key": m.key,
        "name": m.name,
        "formula": m.formula or "",
        "formulaType": m.formula_type,
        "dependsOnMetricIds": m.depends_on_metric_ids or [],
        "version": m.version,
        "datasourceId": str(m.datasource_id) if m.datasource_id else None,
        "aggKind": m.agg_kind,
        "tableRef": m.table_ref,
    }


@router.get("")
async def list_metrics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricDefinition)
        .where(_visible_scope(current_user.id), MetricDefinition.active.is_(True))
        .order_by(MetricDefinition.created_at.desc())
    )
    items = list(result.scalars().all())
    return SuccessResponse(
        data=[MetricResponse.model_validate(m).model_dump(mode="json", by_alias=True) for m in items]
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_metric(
    body: MetricCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    # 免手写 SQL：用户选了「字段 + 聚合方式」时自动生成 SUM("字段") 式公式
    formula = body.formula
    if not formula and body.source_field and body.agg:
        formula = '{}("{}")'.format(body.agg, body.source_field)
    if not formula:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_METRIC", "message": "请提供计算公式，或选择字段和聚合方式自动生成"},
        )
    agg_kind = body.agg_kind or (body.agg if body.agg else None)
    try:
        assert_formula_allowed(formula)
    except MetricServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_METRIC", "message": str(e)},
        )
    existing = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.key == body.key,
            MetricDefinition.user_id == current_user.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "DUPLICATE_METRIC", "message": f"指标 key 已存在: {body.key}"},
        )
    # 依据公式中的 metric('key') 引用判断派生关系，并回填依赖（当前用户下的指标 ID）
    formula_type, dep_ids = await _infer_metric_meta(db, current_user.id, formula)
    metric = MetricDefinition(
        user_id=current_user.id,
        key=body.key,
        name=body.name,
        description=body.description,
        formula=formula,
        formula_type=formula_type,
        depends_on_metric_ids=dep_ids or None,
        agg_kind=agg_kind,
        datasource_id=body.datasource_id,
        table_ref=body.table_ref,
    )
    db.add(metric)
    await db.flush()
    await db.refresh(metric)
    uow = UnitOfWork(db)
    service = MetricService(uow)
    await service._extract_lineage(metric.id, metric.formula)
    return SuccessResponse(
        data=MetricResponse.model_validate(metric).model_dump(mode="json", by_alias=True)
    )


@router.patch("/{metric_id}")
async def update_metric(
    metric_id: UUID,
    body: MetricUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            MetricDefinition.user_id == current_user.id,
        )
    )
    metric = result.scalar_one_or_none()
    if metric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "指标不存在"},
        )
    old_formula = metric.formula
    if body.formula is not None:
        try:
            assert_formula_allowed(body.formula)
        except MetricServiceError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_METRIC", "message": str(e)},
            )
    for field in ("name", "description", "formula", "agg_kind", "table_ref", "active"):
        if getattr(body, field, None) is not None:
            setattr(metric, field, getattr(body, field))
    if body.datasource_id is not None:
        metric.datasource_id = body.datasource_id
    if body.formula is not None and body.formula != old_formula:
        # 公式变更后重新推断派生关系，避免基础/派生类型与依赖失配
        formula_type, dep_ids = await _infer_metric_meta(db, current_user.id, metric.formula)
        metric.formula_type = formula_type
        metric.depends_on_metric_ids = dep_ids or None
    await db.flush()
    await db.refresh(metric)
    if body.formula is not None and body.formula != old_formula:
        uow = UnitOfWork(db)
        service = MetricService(uow)
        await service._extract_lineage(metric.id, metric.formula)
        await _trigger_dependent_dashboards(metric.id, db)
    return SuccessResponse(
        data=MetricResponse.model_validate(metric).model_dump(mode="json", by_alias=True)
    )


@router.delete("/{metric_id}")
async def delete_metric(
    metric_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            MetricDefinition.user_id == current_user.id,
        )
    )
    metric = result.scalar_one_or_none()
    if metric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "指标不存在"},
        )
    await db.execute(delete(MetricDefinition).where(MetricDefinition.id == metric_id))
    await db.flush()
    return SuccessResponse(data={"message": "已删除"})


class _PublishVersionRequest(BaseModel):
    change_note: str


class _RollbackRequest(BaseModel):
    version: int


@router.get("/{metric_id}/dependencies")
async def get_metric_dependencies(
    metric_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            _visible_scope(current_user.id),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "指标不存在"},
        )
    uow = UnitOfWork(db)
    service = MetricService(uow)
    dep_ids = await service.resolve_dependencies(metric_id)
    if dep_ids:
        rows = await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.id.in_([UUID(x) for x in dep_ids])
            )
        )
        deps = list(rows.scalars().all())
    else:
        deps = []
    return SuccessResponse(data=[_metric_dict(m) for m in deps])


@router.get("/{metric_id}/dependents")
async def get_metric_dependents(
    metric_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            _visible_scope(current_user.id),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "指标不存在"},
        )
    rows = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.active.is_(True),
            _visible_scope(current_user.id),
        )
    )
    dependents = [
        m
        for m in rows.scalars().all()
        if str(metric_id) in (m.depends_on_metric_ids or [])
    ]
    return SuccessResponse(data=[_metric_dict(m) for m in dependents])


@router.get("/{metric_id}/impact")
async def get_metric_impact(
    metric_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    uow = UnitOfWork(db)
    service = MetricService(uow)
    try:
        report = await service.impact_analysis(metric_id)
    except MetricServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(e)},
        )
    return SuccessResponse(data=report)


@router.get("/{metric_id}/lineage")
async def get_metric_lineage(
    metric_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.id == metric_id,
            _visible_scope(current_user.id),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "指标不存在"},
        )
    uow = UnitOfWork(db)
    service = MetricService(uow)
    lineage = await service.get_lineage(metric_id)
    return SuccessResponse(data=lineage)


@router.get("/{metric_id}/versions")
async def list_metric_versions(
    metric_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(MetricVersion)
        .where(MetricVersion.metric_id == metric_id)
        .order_by(MetricVersion.version.asc())
    )
    versions = list(result.scalars().all())
    return SuccessResponse(
        data=[
            {
                "version": v.version,
                "change_note": v.change_note,
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in versions
        ]
    )


@router.post("/{metric_id}/versions", status_code=status.HTTP_201_CREATED)
async def publish_metric_version(
    metric_id: UUID,
    body: _PublishVersionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    uow = UnitOfWork(db)
    service = MetricService(uow)
    try:
        version = await service.publish_version(metric_id, body.change_note, current_user.id)
    except MetricServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(e)},
        )
    await _trigger_dependent_dashboards(metric_id, db)
    return SuccessResponse(
        data={
            "id": str(version.id),
            "metric_id": str(version.metric_id),
            "version": version.version,
            "change_note": version.change_note,
            "created_by": str(version.created_by) if version.created_by else None,
            "created_at": version.created_at.isoformat() if version.created_at else None,
        }
    )


@router.post("/{metric_id}/rollback")
async def rollback_metric(
    metric_id: UUID,
    body: _RollbackRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    uow = UnitOfWork(db)
    service = MetricService(uow)
    try:
        metric = await service.rollback_to(metric_id, body.version, current_user.id)
    except MetricServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(e)},
        )
    await _trigger_dependent_dashboards(metric_id, db)
    return SuccessResponse(
        data=MetricResponse.model_validate(metric).model_dump(mode="json", by_alias=True)
    )


@router.get("/{metric_id}/versions/compare")
async def compare_metric_versions(
    metric_id: UUID,
    from_version: int,
    to_version: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    uow = UnitOfWork(db)
    service = MetricService(uow)
    try:
        diff = await service.compare_versions(metric_id, from_version, to_version)
    except MetricServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(e)},
        )
    return SuccessResponse(data=diff)