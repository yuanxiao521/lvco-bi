import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.metric import MetricDefinition
from app.models.user import User
from app.schemas import (
    MetricCreate,
    MetricResponse,
    MetricUpdate,
    SuccessResponse,
)
from app.services.metric_service import MetricServiceError, assert_formula_allowed

router = APIRouter(prefix="/metrics", tags=["指标中心"])

logger = logging.getLogger("lvco.metrics")


def _visible_scope(user_id: UUID):
    """指标可见范围：用户私有 + 全局/公开模板。"""
    return (MetricDefinition.user_id == user_id) | (MetricDefinition.user_id.is_(None))


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
    try:
        assert_formula_allowed(body.formula)
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
    metric = MetricDefinition(
        user_id=current_user.id,
        key=body.key,
        name=body.name,
        description=body.description,
        formula=body.formula,
        agg_kind=body.agg_kind,
        datasource_id=body.datasource_id,
        table_ref=body.table_ref,
    )
    db.add(metric)
    await db.flush()
    await db.refresh(metric)
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
    await db.flush()
    await db.refresh(metric)
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