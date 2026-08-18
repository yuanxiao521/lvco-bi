"""日志审计 API：查询 operation_logs，支持按用户、操作类型、资源类型、状态码、时间筛选。"""
import csv
import io
import math
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.operation_log import OperationLog
from app.models.user import User
from app.schemas import CamelModel, SuccessResponse

router = APIRouter(prefix="/audit", tags=["日志审计"])


class OperationLogItem(CamelModel):
    id: UUID
    user_id: UUID | None
    user_email: str | None = None
    user_display_name: str | None = None
    action: str
    resource_type: str
    resource_id: UUID | None
    method: str
    path: str
    status_code: int
    duration_ms: int
    ip_address: str | None
    user_agent: str | None
    created_at: str


class OperationLogListResponse(CamelModel):
    items: list[OperationLogItem]
    total: int
    page: int
    page_size: int
    pages: int


@router.get("/logs")
async def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user_id: UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    status_code: int | None = None,
    method: str | None = None,
    search: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """查询操作日志（任何登录用户可访问，便于自助审计）。"""
    conditions = []
    if user_id is not None:
        conditions.append(OperationLog.user_id == user_id)
    if action:
        # 支持 action 前缀匹配，如 'canvas.' 匹配全部 canvas 操作
        if action.endswith("."):
            conditions.append(OperationLog.action.like(f"{action}%"))
        else:
            conditions.append(OperationLog.action == action)
    if resource_type:
        conditions.append(OperationLog.resource_type == resource_type)
    if status_code is not None:
        conditions.append(OperationLog.status_code == status_code)
    if method:
        conditions.append(OperationLog.method == method.upper())
    if search:
        like = f"%{search}%"
        conditions.append(or_(
            OperationLog.path.ilike(like),
            OperationLog.action.ilike(like),
        ))
    if start_at is not None:
        conditions.append(OperationLog.created_at >= start_at)
    if end_at is not None:
        conditions.append(OperationLog.created_at <= end_at)

    base_q = select(OperationLog)
    count_q = select(func.count()).select_from(OperationLog)
    if conditions:
        base_q = base_q.where(and_(*conditions))
        count_q = count_q.where(and_(*conditions))

    total = (await db.execute(count_q)).scalar() or 0

    base_q = base_q.order_by(OperationLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(base_q)).scalars().all()

    # 关联用户信息
    user_ids = {r.user_id for r in rows if r.user_id}
    user_map: dict = {}
    if user_ids:
        from app.models.user import User
        user_rows = (await db.execute(
            select(User).where(User.id.in_(user_ids))
        )).scalars().all()
        user_map = {u.id: u for u in user_rows}

    items = []
    for r in rows:
        u = user_map.get(r.user_id) if r.user_id else None
        items.append(OperationLogItem(
            id=r.id,
            user_id=r.user_id,
            user_email=u.email if u else None,
            user_display_name=u.display_name if u else None,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            method=r.method,
            path=r.path,
            status_code=r.status_code,
            duration_ms=r.duration_ms,
            ip_address=r.ip_address,
            user_agent=r.user_agent,
            created_at=r.created_at.isoformat() if r.created_at else "",
        ))

    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(data=OperationLogListResponse(
        items=items, total=total, page=page, page_size=page_size, pages=pages,
    ).model_dump(mode="json", by_alias=True))


@router.get("/logs/summary")
async def logs_summary(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """汇总最近 24 小时的操作分布（用于 dashboard 顶部统计）。"""
    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    total_q = select(func.count()).select_from(OperationLog).where(OperationLog.created_at >= since)
    total = (await db.execute(total_q)).scalar() or 0

    by_action_rows = (await db.execute(
        select(OperationLog.action, func.count(OperationLog.id))
        .where(OperationLog.created_at >= since)
        .group_by(OperationLog.action)
        .order_by(func.count(OperationLog.id).desc())
        .limit(10)
    )).all()

    by_resource_rows = (await db.execute(
        select(OperationLog.resource_type, func.count(OperationLog.id))
        .where(OperationLog.created_at >= since)
        .group_by(OperationLog.resource_type)
        .order_by(func.count(OperationLog.id).desc())
    )).all()

    error_count = (await db.execute(
        select(func.count()).select_from(OperationLog).where(
            OperationLog.created_at >= since,
            OperationLog.status_code >= 400,
        )
    )).scalar() or 0

    return SuccessResponse(data={
        "total24h": total,
        "error24h": error_count,
        "byAction": [{"action": a, "count": c} for a, c in by_action_rows],
        "byResource": [{"resourceType": r, "count": c} for r, c in by_resource_rows],
        "since": since.isoformat(),
    })


@router.get("/logs/export")
async def export_logs_csv(
    user_id: UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    status_code: int | None = None,
    method: str | None = None,
    search: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """导出日志为 CSV（按当前筛选条件）。"""
    conditions = []
    if user_id is not None:
        conditions.append(OperationLog.user_id == user_id)
    if action:
        conditions.append(
            OperationLog.action.like(f"{action}%")
            if action.endswith(".")
            else OperationLog.action == action
        )
    if resource_type:
        conditions.append(OperationLog.resource_type == resource_type)
    if status_code is not None:
        conditions.append(OperationLog.status_code == status_code)
    if method:
        conditions.append(OperationLog.method == method.upper())
    if search:
        like = f"%{search}%"
        conditions.append(or_(
            OperationLog.path.ilike(like),
            OperationLog.action.ilike(like),
        ))
    if start_at is not None:
        conditions.append(OperationLog.created_at >= start_at)
    if end_at is not None:
        conditions.append(OperationLog.created_at <= end_at)

    q = select(OperationLog)
    if conditions:
        q = q.where(and_(*conditions))
    q = q.order_by(OperationLog.created_at.desc()).limit(10000)  # 防止单次导出过大
    rows = (await db.execute(q)).scalars().all()

    user_ids = {r.user_id for r in rows if r.user_id}
    user_map: dict = {}
    if user_ids:
        from app.models.user import User as UserModel
        user_rows = (await db.execute(
            select(UserModel).where(UserModel.id.in_(user_ids))
        )).scalars().all()
        user_map = {u.id: u for u in user_rows}

    # 生成 CSV（用 BOM 头让 Excel 正确识别 UTF-8）
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow([
        "时间", "操作者", "邮箱", "动作", "资源类型", "资源ID",
        "方法", "路径", "状态码", "耗时(ms)", "IP", "User-Agent",
    ])
    for r in rows:
        u = user_map.get(r.user_id) if r.user_id else None
        writer.writerow([
            r.created_at.isoformat() if r.created_at else "",
            u.display_name if u else "",
            u.email if u else "",
            r.action,
            r.resource_type,
            str(r.resource_id) if r.resource_id else "",
            r.method,
            r.path,
            r.status_code,
            r.duration_ms,
            r.ip_address or "",
            (r.user_agent or "")[:200],
        ])

    filename = f"audit_logs_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
