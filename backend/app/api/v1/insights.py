"""智能洞察 API"""
import math
import uuid
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.datasource import DataSource, SourceType
from app.models.insight_record import InsightRecord
from app.models.insight_rule import InsightRule, RunStatus
from app.models.insight_suggestion import InsightSuggestion, SuggestionStatus
from app.models.user import User
from app.schemas import SuccessResponse
from app.schemas.insight import (
    InsightRecordRunRequest,
    InsightRecordRunResponse,
    InsightRuleCreate,
    InsightRuleListResponse,
    InsightRuleResponse,
    InsightRuleUpdate,
    InsightSuggestionAccept,
    InsightSuggestionListResponse,
    InsightSuggestionResponse,
)
from app.services.insight_engine.auto_discovery import scan_datasource

router = APIRouter(prefix="/insights", tags=["智能洞察"])


# ============ Rules CRUD ============
@router.get("/rules")
async def list_rules(
    enabled: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    q = select(InsightRule).where(InsightRule.user_id == current_user.id)
    if enabled is not None:
        q = q.where(InsightRule.enabled == enabled)

    total_result = await db.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = total_result.scalar() or 0

    q = q.order_by(InsightRule.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    rules = result.scalars().all()

    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(
        data=InsightRuleListResponse(
            items=[InsightRuleResponse.model_validate(r) for r in rules],
            total=total,
            page=page,
            page_size=page_size,
        ).model_dump(mode="json", by_alias=True)
    )


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: InsightRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    # Verify datasource belongs to user
    datasource = await _get_user_datasource(db, body.datasource_id, current_user.id)
    rule = InsightRule(
        user_id=current_user.id,
        datasource_id=body.datasource_id,
        name=body.name,
        description=body.description,
        query_config=body.query_config.model_dump(mode="json", by_alias=True),
        detect_types=body.detect_types,
        threshold=body.threshold,
        report_type=body.report_type,
        schedule=body.schedule,
        schedule_time=body.schedule_time,
        enabled=body.enabled,
        next_run_at=_compute_next_run(body.schedule_time),
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return SuccessResponse(
        data=InsightRuleResponse.model_validate(rule).model_dump(mode="json", by_alias=True)
    )


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = await _get_user_rule(db, rule_id, current_user.id)
    return SuccessResponse(
        data=InsightRuleResponse.model_validate(rule).model_dump(mode="json", by_alias=True)
    )


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    body: InsightRuleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = await _get_user_rule(db, rule_id, current_user.id)
    update_data = body.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        if k == "query_config" and v is not None:
            rule.query_config = v
        else:
            setattr(rule, k, v)
    if body.schedule_time is not None:
        rule.next_run_at = _compute_next_run(body.schedule_time)
    await db.commit()
    await db.refresh(rule)
    return SuccessResponse(
        data=InsightRuleResponse.model_validate(rule).model_dump(mode="json", by_alias=True)
    )


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    rule = await _get_user_rule(db, rule_id, current_user.id)
    await db.delete(rule)
    await db.commit()
    return Response(status_code=204)


@router.post("/rules/{rule_id}/run")
async def run_rule_now(
    rule_id: uuid.UUID,
    body: InsightRecordRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = await _get_user_rule(db, rule_id, current_user.id)
    now = datetime.utcnow()
    period_end = body.period_end or now
    period_start = body.period_start or _default_period_start(rule, period_end)
    record = InsightRecord(
        rule_id=rule.id,
        user_id=current_user.id,
        datasource_id=rule.datasource_id,
        run_at=now,
        period_start=period_start,
        period_end=period_end,
        status="pending",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    # TODO(Phase 4): trigger async InsightRunner here
    return SuccessResponse(
        data=InsightRecordRunResponse(
            record_id=record.id, status="pending"
        ).model_dump(mode="json", by_alias=True)
    )


# ============ Suggestions ============
@router.post("/discover/{datasource_id}")
async def discover_datasource(
    datasource_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """Scan a datasource schema and create InsightSuggestion records."""
    datasource = await _get_user_datasource(db, datasource_id, current_user.id)
    if datasource.source_type != SourceType.postgresql:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UNSUPPORTED_SOURCE", "message": "目前仅支持 PostgreSQL 数据源的自动发现"},
        )
    suggestions = await scan_datasource(db, datasource, current_user.id)
    return SuccessResponse(
        data={
            "suggestionsCreated": len(suggestions),
            "suggestions": [
                InsightSuggestionResponse.model_validate(s).model_dump(mode="json", by_alias=True)
                for s in suggestions
            ],
        }
    )


@router.get("/suggestions")
async def list_suggestions(
    suggestion_status: str | None = Query(None, alias="status"),
    datasource_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    q = select(InsightSuggestion).where(InsightSuggestion.user_id == current_user.id)
    if suggestion_status is not None:
        q = q.where(InsightSuggestion.status == suggestion_status)
    if datasource_id is not None:
        q = q.where(InsightSuggestion.datasource_id == datasource_id)
    q = q.order_by(InsightSuggestion.created_at.desc())
    result = await db.execute(q)
    suggestions = result.scalars().all()
    return SuccessResponse(
        data=InsightSuggestionListResponse(
            items=[InsightSuggestionResponse.model_validate(s) for s in suggestions],
            total=len(suggestions),
        ).model_dump(mode="json", by_alias=True)
    )


@router.post("/suggestions/{suggestion_id}/accept")
async def accept_suggestion(
    suggestion_id: uuid.UUID,
    body: InsightSuggestionAccept,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    suggestion = await _get_user_suggestion(db, suggestion_id, current_user.id)
    if suggestion.status != SuggestionStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ALREADY_ACTED", "message": "该建议已处理"},
        )
    config = suggestion.suggested_config or {}
    rule = InsightRule(
        user_id=current_user.id,
        datasource_id=suggestion.datasource_id,
        name=body.name or suggestion.suggested_name or f"{suggestion.table_name} 日报",
        query_config=config,
        detect_types=body.detect_types or ["anomaly", "trend", "ratio"],
        report_type="daily_report",
        schedule="daily",
        schedule_time=body.schedule_time or time(9, 0, 0),
        enabled=body.enabled,
        auto_created=True,
        next_run_at=_compute_next_run(body.schedule_time or time(9, 0, 0)),
    )
    db.add(rule)
    await db.flush()
    suggestion.status = SuggestionStatus.accepted
    suggestion.accepted_rule_id = rule.id
    suggestion.acted_at = datetime.utcnow()
    await db.commit()
    await db.refresh(rule)
    return SuccessResponse(
        data=InsightRuleResponse.model_validate(rule).model_dump(mode="json", by_alias=True)
    )


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    suggestion_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    suggestion = await _get_user_suggestion(db, suggestion_id, current_user.id)
    if suggestion.status != SuggestionStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ALREADY_ACTED", "message": "该建议已处理"},
        )
    suggestion.status = SuggestionStatus.dismissed
    suggestion.acted_at = datetime.utcnow()
    await db.commit()
    return SuccessResponse(data={"status": "dismissed"})


# ============ Helpers ============
async def _get_user_rule(db: AsyncSession, rule_id: uuid.UUID, user_id: uuid.UUID) -> InsightRule:
    result = await db.execute(
        select(InsightRule).where(InsightRule.id == rule_id, InsightRule.user_id == user_id)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "规则不存在"},
        )
    return rule


async def _get_user_datasource(db: AsyncSession, datasource_id: uuid.UUID, user_id: uuid.UUID) -> DataSource:
    result = await db.execute(
        select(DataSource).where(DataSource.id == datasource_id, DataSource.user_id == user_id)
    )
    ds = result.scalar_one_or_none()
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return ds


async def _get_user_suggestion(db: AsyncSession, suggestion_id: uuid.UUID, user_id: uuid.UUID) -> InsightSuggestion:
    result = await db.execute(
        select(InsightSuggestion).where(
            InsightSuggestion.id == suggestion_id, InsightSuggestion.user_id == user_id
        )
    )
    s = result.scalar_one_or_none()
    if s is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "建议不存在"},
        )
    return s


def _compute_next_run(schedule_time: time) -> datetime:
    """计算下次运行时间（简化为下一个该时刻）"""
    now = datetime.utcnow()
    candidate = now.replace(
        hour=schedule_time.hour,
        minute=schedule_time.minute,
        second=schedule_time.second,
        microsecond=0,
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _default_period_start(rule: InsightRule, period_end: datetime) -> datetime:
    config = rule.query_config or {}
    days = config.get("time_range_days", 30) if isinstance(config, dict) else 30
    return period_end - timedelta(days=days)
