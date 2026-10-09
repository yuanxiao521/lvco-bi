"""公开访问 API — 无需认证"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.dashboard import Dashboard
from app.models.report import Report
from app.schemas import SuccessResponse

public_router = APIRouter(prefix="/public", tags=["公开访问"])


@public_router.get("/share/{token}")
async def view_shared(token: str, db: AsyncSession = Depends(get_db)):
    """通过 share_token 查看公开分享的仪表盘或报表"""

    # Try Dashboard first
    result = await db.execute(select(Dashboard).where(Dashboard.share_token == token))
    dashboard = result.scalar_one_or_none()
    if dashboard:
        charts = []
        for dc in dashboard.dashboard_charts:
            charts.append({
                "chart_type": dc.chart_config.chart_type if dc.chart_config else None,
                "query_config": dc.chart_config.query_config if dc.chart_config else None,
            })
        return SuccessResponse(data={
            "type": "dashboard",
            "title": dashboard.title,
            "charts": charts,
        })

    # Try Report
    result = await db.execute(select(Report).where(Report.share_token == token))
    report = result.scalar_one_or_none()
    if report:
        return SuccessResponse(data={
            "type": "report",
            "title": report.title,
            "blocks": report.snapshot_blocks,
        })

    raise HTTPException(status_code=404, detail="分享链接不存在或已失效")
