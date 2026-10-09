"""回收站 API：浏览、恢复、彻底删除"""
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.canvas import Canvas
from app.models.dashboard import Dashboard
from app.models.report import Report, ReportStatus
from app.models.user import User, UserRole
from app.schemas import SuccessResponse

router = APIRouter(prefix="/trash", tags=["回收站"])
_log = logging.getLogger("lvco.trash")


@router.get("")
async def list_trash(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """列出当前用户回收站中的所有资源"""
    items: list[dict] = []

    # Deleted canvases
    canvas_result = await db.execute(
        select(Canvas).where(
            Canvas.user_id == current_user.id,
            Canvas.deleted_at.isnot(None),
        ).order_by(Canvas.deleted_at.desc())
    )
    for c in canvas_result.scalars().all():
        items.append({
            "id": str(c.id),
            "type": "canvas",
            "title": c.title,
            "deletedAt": c.deleted_at.isoformat() if c.deleted_at else None,
        })

    # Deleted dashboards
    dashboard_result = await db.execute(
        select(Dashboard).where(
            Dashboard.user_id == current_user.id,
            Dashboard.deleted_at.isnot(None),
        ).order_by(Dashboard.deleted_at.desc())
    )
    for d in dashboard_result.scalars().all():
        items.append({
            "id": str(d.id),
            "type": "dashboard",
            "title": d.title,
            "deletedAt": d.deleted_at.isoformat() if d.deleted_at else None,
        })

    # Deleted reports
    report_result = await db.execute(
        select(Report).where(
            Report.user_id == current_user.id,
            Report.status == ReportStatus.deleted,
        ).order_by(Report.updated_at.desc())
    )
    for r in report_result.scalars().all():
        items.append({
            "id": str(r.id),
            "type": "report",
            "title": r.title,
            "deletedAt": r.updated_at.isoformat() if r.updated_at else None,
        })

    # 注意：数据源采用硬删除（不可恢复），不进回收站

    return SuccessResponse(data={"items": items, "total": len(items)})


@router.post("/{item_type}/{item_id}/restore")
async def restore_trash_item(
    item_type: str,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """恢复回收站中的资源"""
    if item_type == "canvas":
        result = await db.execute(
            select(Canvas).where(
                Canvas.id == item_id,
                Canvas.user_id == current_user.id,
                Canvas.deleted_at.isnot(None),
            )
        )
        canvas = result.scalar_one_or_none()
        if canvas is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未找到该画布"})
        canvas.deleted_at = None
        await db.flush()
        await db.commit()
        return SuccessResponse(data={"message": "画布已恢复"})

    elif item_type == "dashboard":
        result = await db.execute(
            select(Dashboard).where(
                Dashboard.id == item_id,
                Dashboard.user_id == current_user.id,
                Dashboard.deleted_at.isnot(None),
            )
        )
        dashboard = result.scalar_one_or_none()
        if dashboard is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未找到该仪表盘"})
        dashboard.deleted_at = None
        await db.flush()
        await db.commit()
        return SuccessResponse(data={"message": "仪表盘已恢复"})

    elif item_type == "report":
        result = await db.execute(
            select(Report).where(
                Report.id == item_id,
                Report.user_id == current_user.id,
                Report.status == ReportStatus.deleted,
            )
        )
        report = result.scalar_one_or_none()
        if report is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未找到该报表"})
        report.status = ReportStatus.draft
        await db.flush()
        await db.commit()
        return SuccessResponse(data={"message": "报表已恢复"})

    else:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TYPE", "message": f"不支持的类型: {item_type}"})


@router.delete("/{item_type}/{item_id}")
async def permanent_delete(
    item_type: str,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """彻底删除回收站中的资源（不可恢复）— 仅 admin 可执行"""
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "彻底删除仅限管理员操作，请联系管理员处理"},
        )
    if item_type == "canvas":
        result = await db.execute(
            select(Canvas).where(
                Canvas.id == item_id,
                Canvas.user_id == current_user.id,
                Canvas.deleted_at.isnot(None),
            )
        )
        canvas = result.scalar_one_or_none()
        if canvas is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未找到该画布"})
        await db.delete(canvas)
        await db.flush()
        await db.commit()
        return SuccessResponse(data={"message": "画布已彻底删除"})

    elif item_type == "dashboard":
        result = await db.execute(
            select(Dashboard).where(
                Dashboard.id == item_id,
                Dashboard.user_id == current_user.id,
                Dashboard.deleted_at.isnot(None),
            )
        )
        dashboard = result.scalar_one_or_none()
        if dashboard is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未找到该仪表盘"})
        await db.delete(dashboard)
        await db.flush()
        await db.commit()
        return SuccessResponse(data={"message": "仪表盘已彻底删除"})

    elif item_type == "report":
        result = await db.execute(
            select(Report).where(
                Report.id == item_id,
                Report.user_id == current_user.id,
                Report.status == ReportStatus.deleted,
            )
        )
        report = result.scalar_one_or_none()
        if report is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未找到该报表"})
        await db.delete(report)
        await db.flush()
        await db.commit()
        return SuccessResponse(data={"message": "报表已彻底删除"})

    else:
        raise HTTPException(status_code=400, detail={"code": "INVALID_TYPE", "message": f"不支持的类型: {item_type}"})
