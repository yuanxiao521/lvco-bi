import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.chart_config import ChartConfig
from app.models.dashboard import Dashboard
from app.models.user import User
from app.schemas import CamelModel, SuccessResponse
from app.services.dashboard_service import DashboardService

router = APIRouter(prefix="/dashboards", tags=["仪表盘"])


class DashboardCreateBody(CamelModel):
    title: str = Field(..., max_length=200)
    description: str | None = None


class DashboardLayoutBody(CamelModel):
    layout: list = Field(default_factory=list)


class DashboardAddChartBody(CamelModel):
    chart_config_id: UUID
    title: str | None = None
    position: dict | None = None


def _summary(d: Dashboard, owner_name: str | None = None, chart_count: int = 0) -> dict:
    owner_id = str(d.user_id) if d.user_id else None
    return {
        "id": str(d.id),
        "title": d.title,
        "description": d.description,
        "chartCount": chart_count,
        "ownerName": owner_name,
        "ownerId": owner_id,
        "createdAt": d.created_at.isoformat() if d.created_at else None,
        "updatedAt": d.updated_at.isoformat() if d.updated_at else None,
    }


@router.get("")
async def list_dashboards(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    items, total = await service.list_dashboards(current_user.id, page, page_size, search)
    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(
        data={
            "items": [_summary(item, owner_name=current_user.display_name, chart_count=len(item.dashboard_charts)) for item in items],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "pages": pages,
        }
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_dashboard(
    body: DashboardCreateBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    dashboard = await service.create(
        user_id=current_user.id,
        title=body.title,
        description=body.description,
    )
    return SuccessResponse(data=_summary(dashboard, owner_name=current_user.display_name))


@router.get("/{dashboard_id}")
async def get_dashboard(
    dashboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    dashboard = await service.get_by_id(dashboard_id, current_user.id)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )

    chart_configs = []
    for dc in dashboard.dashboard_charts:
        # 通过查询加载 chart_config 避免懒加载
        cc_result = await db.execute(select(ChartConfig).where(ChartConfig.id == dc.chart_config_id))
        cc = cc_result.scalar_one_or_none()
        chart_configs.append({
            "id": str(dc.id),
            "title": dc.title,
            "chartType": cc.chart_type.value if cc else None,
            "config": cc.query_config if cc else None,
            "position": dc.position,
        })

    return SuccessResponse(
        data={
            "id": str(dashboard.id),
            "title": dashboard.title,
            "description": dashboard.description,
            "layout": dashboard.layout,
            "chartConfigs": chart_configs,
            "createdAt": dashboard.created_at.isoformat() if dashboard.created_at else None,
            "updatedAt": dashboard.updated_at.isoformat() if dashboard.updated_at else None,
        }
    )


@router.put("/{dashboard_id}/layout")
async def update_layout(
    dashboard_id: UUID,
    body: DashboardLayoutBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    dashboard = await service.update_layout(dashboard_id, current_user.id, body.layout)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    return SuccessResponse(data=_summary(dashboard))


@router.post("/{dashboard_id}/charts", status_code=status.HTTP_201_CREATED)
async def add_chart(
    dashboard_id: UUID,
    body: DashboardAddChartBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    dc = await service.add_chart(
        dashboard_id=dashboard_id,
        user_id=current_user.id,
        chart_config_id=body.chart_config_id,
        title=body.title,
        position=body.position,
    )
    if dc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘或图表配置不存在"},
        )
    return SuccessResponse(
        data={
            "chartId": str(dc.id),
            "dashboardId": str(dashboard_id),
            "title": dc.title,
            "position": dc.position,
        }
    )


@router.delete("/{dashboard_id}/charts/{chart_id}")
async def remove_chart(
    dashboard_id: UUID,
    chart_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    deleted = await service.remove_chart(dashboard_id, chart_id, current_user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "图表不存在"},
        )
    return SuccessResponse(data={"message": "已移除"})


@router.get("/{dashboard_id}/data")
async def get_dashboard_data(
    dashboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    payload = await service.get_dashboard_data(dashboard_id, current_user.id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    return SuccessResponse(data=payload)


@router.post("/{dashboard_id}/refresh")
async def refresh_dashboard(
    dashboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    ok = await service.refresh(dashboard_id, current_user.id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    payload = await service.get_dashboard_data(dashboard_id, current_user.id, use_cache=False)
    return SuccessResponse(data=payload)


@router.post("/{dashboard_id}/share")
async def share_dashboard(
    dashboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    dashboard = await service.share(dashboard_id, current_user.id)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )

    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    return SuccessResponse(
        data={
            "shareToken": dashboard.share_token,
            "shareUrl": f"/share/{dashboard.share_token}",
            "expiresAt": expires_at.isoformat(),
        }
    )


@router.delete("/{dashboard_id}")
async def delete_dashboard(
    dashboard_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DashboardService(db)
    deleted = await service.delete(dashboard_id, current_user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    return SuccessResponse(data={"message": "已删除"})