import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_dashboard_service, require_editor
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


class ScheduleRefreshBody(CamelModel):
    cron: str
    enabled: bool


class ManualRefreshBody(CamelModel):
    async_mode: bool = False


class ShareLinkBody(CamelModel):
    """分享链接期限参数（三态语义，显式写死，避免再出现"接口现编 expiresAt"）。

    expiresInDays：
      - **整个字段不传**（含不传 body）→ 默认 30 天（向后兼容：老前端 POST 空 body）
      - **传 null**                   → 永久有效（share_expires_at = NULL）
      - **传 0**                      → 永久有效（与 null 同义，方便前端用数字表达）
      - **传 N > 0**                  → now + N 天（上限 3650，防手滑填 99999）

    "不传" 与 "显式传 null" 的区分靠 pydantic 的 model_fields_set，不是靠 None 值——
    否则老前端（不带字段）会被误判成"永久"，那是最危险的方向。
    """

    expires_in_days: int | None = Field(default=None, ge=0, le=3650)


SHARE_DEFAULT_DAYS = 30


def _requested_share_days(body: "ShareLinkBody | None") -> int:
    """把请求体解析成"有效天数"（0 = 永久）。三态语义见 ShareLinkBody docstring。"""
    if body is None or "expires_in_days" not in body.model_fields_set:
        return SHARE_DEFAULT_DAYS  # 没传 → 默认 30 天
    if body.expires_in_days is None:
        return 0  # 显式 null → 永久
    return body.expires_in_days


def _resolve_share_expiry(days: int) -> datetime | None:
    """天数 → 绝对过期时间；0 表示永久有效（落 NULL）。"""
    if days == 0:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


def _summary(d: Dashboard, owner_name: str | None = None, chart_count: int = 0) -> dict:
    owner_id = str(d.user_id) if d.user_id else None
    return {
        "id": str(d.id),
        "title": d.title,
        "description": d.description,
        "chartCount": chart_count,
        "ownerName": owner_name,
        "ownerId": owner_id,
        # 分享链接过期时间：从库里读（NULL = 永久有效或未分享）；不再由接口现编
        "shareExpiresAt": (
            d.share_expires_at.isoformat()
            if isinstance(getattr(d, "share_expires_at", None), datetime)
            else None
        ),
        "createdAt": d.created_at.isoformat() if d.created_at else None,
        "updatedAt": d.updated_at.isoformat() if d.updated_at else None,
    }


@router.get("")
async def list_dashboards(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = None,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_editor)])
async def create_dashboard(
    body: DashboardCreateBody,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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


@router.put("/{dashboard_id}/layout", dependencies=[Depends(require_editor)])
async def update_layout(
    dashboard_id: UUID,
    body: DashboardLayoutBody,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    dashboard = await service.update_layout(dashboard_id, current_user.id, body.layout)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    return SuccessResponse(data=_summary(dashboard))


@router.post("/{dashboard_id}/charts", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_editor)])
async def add_chart(
    dashboard_id: UUID,
    body: DashboardAddChartBody,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
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
    from app.services.metric_service import extract_metric_ids_from_measures, record_metric_usage
    cc_result = await db.execute(select(ChartConfig).where(ChartConfig.id == body.chart_config_id))
    cc = cc_result.scalar_one_or_none()
    if cc is not None:
        measures = (cc.query_config or {}).get("measures", [])
        for mid_str in extract_metric_ids_from_measures(measures):
            await record_metric_usage(
                db, UUID(mid_str), usage_type="dashboard",
                usage_ref_id=str(dashboard_id), user_id=current_user.id,
            )
    return SuccessResponse(
        data={
            "chartId": str(dc.id),
            "dashboardId": str(dashboard_id),
            "title": dc.title,
            "position": dc.position,
        }
    )


@router.delete("/{dashboard_id}/charts/{chart_id}", dependencies=[Depends(require_editor)])
async def remove_chart(
    dashboard_id: UUID,
    chart_id: UUID,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    payload = await service.get_dashboard_data(dashboard_id, current_user.id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    return SuccessResponse(data=payload)


@router.post("/{dashboard_id}/schedule", dependencies=[Depends(require_editor)])
async def set_dashboard_schedule(
    dashboard_id: UUID,
    body: ScheduleRefreshBody,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        result = await service.schedule_refresh(dashboard_id, current_user.id, body.cron, body.enabled)
    except ValueError as e:
        msg = str(e)
        if "dashboard_not_found" in msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "Dashboard not found"},
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CRON", "message": "Invalid cron expression"},
        )
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Permission denied"},
        )
    return SuccessResponse(data=result)


@router.delete("/{dashboard_id}/schedule", dependencies=[Depends(require_editor)])
async def disable_dashboard_schedule(
    dashboard_id: UUID,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        result = await service.schedule_refresh(dashboard_id, current_user.id, cron=None, enabled=False)
    except ValueError as e:
        if "dashboard_not_found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "Dashboard not found"},
            )
        raise
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Permission denied"},
        )
    return SuccessResponse(data=result)


@router.post("/{dashboard_id}/refresh")
async def refresh_dashboard(
    dashboard_id: UUID,
    body: ManualRefreshBody | None = None,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    async_mode = body.async_mode if body else False
    try:
        result = await service.manual_refresh(dashboard_id, current_user.id, async_mode)
    except ValueError as e:
        if "dashboard_not_found" in str(e):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "Dashboard not found"},
            )
        raise
    return SuccessResponse(data=result)


@router.post("/{dashboard_id}/share", dependencies=[Depends(require_editor)])
async def share_dashboard(
    dashboard_id: UUID,
    body: ShareLinkBody | None = None,
    service: DashboardService = Depends(get_dashboard_service),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """生成 / 续期分享链接（仅属主）。

    过期时间按 body.expiresInDays 解析后**真的写进 dashboards.share_expires_at**，
    返回的 expiresAt 是 commit 后从库里 refresh 读回来的值——不再是 now()+30d 现编。
    """
    dashboard = await service.share(dashboard_id, current_user.id)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )

    expires_in_days = _requested_share_days(body)
    dashboard.share_expires_at = _resolve_share_expiry(expires_in_days)
    await db.flush()
    await db.commit()
    await db.refresh(dashboard)

    return SuccessResponse(
        data={
            "shareToken": dashboard.share_token,
            "shareUrl": f"/share/{dashboard.share_token}",
            # 库里读回；None = 永久有效
            "expiresAt": (
                dashboard.share_expires_at.isoformat()
                if dashboard.share_expires_at
                else None
            ),
            "permanent": dashboard.share_expires_at is None,
            "expiresInDays": expires_in_days,
        }
    )


@router.delete("/{dashboard_id}/share", dependencies=[Depends(require_editor)])
async def revoke_dashboard_share(
    dashboard_id: UUID,
    service: DashboardService = Depends(get_dashboard_service),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """撤销分享：清空 token 与过期时间，链接立即失效（仅属主）。

    非属主统一 404（不用 403）——与公开访问口径一致，不泄漏"该仪表盘是否存在"。
    """
    dashboard = await service.get_by_id(dashboard_id, current_user.id)
    if dashboard is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )

    dashboard.share_token = None
    dashboard.share_expires_at = None
    dashboard.is_public = False
    await db.flush()
    await db.commit()

    return SuccessResponse(
        data={
            "dashboardId": str(dashboard_id),
            "revoked": True,
            "shareToken": None,
            "expiresAt": None,
        }
    )


@router.delete("/{dashboard_id}", dependencies=[Depends(require_editor)])
async def delete_dashboard(
    dashboard_id: UUID,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    deleted = await service.delete(dashboard_id, current_user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "仪表盘不存在"},
        )
    return SuccessResponse(data={"message": "已删除"})