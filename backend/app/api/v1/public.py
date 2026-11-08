"""公开访问 API — 无需认证

P0 止血（评审 D3）：分享链接必须有"有效性"。

判定顺序（**任何一种失败都返回同一个 404**，避免用状态码泄漏 token 是否存在）：
  1) token 不存在 / 已被撤销（撤销 = share_token 置 NULL，见 DELETE /{res}/{id}/share）
  2) 记录已软删除（dashboard.deleted_at 非空 / report.status = deleted）
  3) 链接已过期（share_expires_at 非空且 <= now；NULL 表示永久有效）
  4) dashboard 不处于公开状态（is_public=False，撤销时的双保险）

实现说明：
  - 过滤条件放在 SQL 里（而不是先查出来再在 Python 里判断），避免"查到了但忘了判"的漏写；
  - 关系加载用 selectinload：异步会话里访问懒加载关系会抛 MissingGreenlet（此前这里是隐性 500）；
  - 语句构造抽成纯函数（dashboard_share_stmt / report_share_stmt），便于单测直接断言 WHERE 条件。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.dashboard import Dashboard
from app.models.dashboard_chart import DashboardChart
from app.models.report import Report, ReportStatus
from app.schemas import SuccessResponse

public_router = APIRouter(prefix="/public", tags=["公开访问"])

# 分享 token 的最大长度（与 models 的 String(100) 对齐）；超长直接视为无效，不落库查询
_MAX_TOKEN_LEN = 100


def _share_not_found() -> HTTPException:
    """链接无效的统一响应。

    故意只给 404、不区分原因：403 会告诉攻击者"这个 token 是真的，只是过期/被撤销"，
    等于送了一个 token 有效性探测接口。
    """
    return HTTPException(status_code=404, detail="分享链接不存在或已失效")


def _not_expired_clause(column, now: datetime):
    """有效期条件：NULL = 永久有效；否则必须晚于 now。"""
    return or_(column.is_(None), column > now)


def dashboard_share_stmt(token: str, now: datetime | None = None):
    """构造"仍有效的 dashboard 分享"查询（纯函数，便于测试断言）。"""
    now = now or datetime.now(timezone.utc)
    return (
        select(Dashboard)
        .where(
            Dashboard.share_token == token,
            Dashboard.is_public.is_(True),
            Dashboard.deleted_at.is_(None),
            _not_expired_clause(Dashboard.share_expires_at, now),
        )
        .options(
            selectinload(Dashboard.dashboard_charts).selectinload(
                DashboardChart.chart_config
            )
        )
    )


def report_share_stmt(token: str, now: datetime | None = None):
    """构造"仍有效的 report 分享"查询（纯函数，便于测试断言）。

    报表的软删除是 status=deleted（models/report.py 没有 deleted_at 列）。
    """
    now = now or datetime.now(timezone.utc)
    return select(Report).where(
        Report.share_token == token,
        Report.status != ReportStatus.deleted,
        _not_expired_clause(Report.share_expires_at, now),
    )


@public_router.get("/share/{token}")
async def view_shared(token: str, db: AsyncSession = Depends(get_db)):
    """通过 share_token 查看公开分享的仪表盘或报表。

    失效（不存在 / 已撤销 / 已过期 / 已软删）一律 404，不返回 403。
    """
    if not token or len(token) > _MAX_TOKEN_LEN:
        raise _share_not_found()

    dashboard = (await db.execute(dashboard_share_stmt(token))).scalar_one_or_none()
    if dashboard is not None:
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

    report = (await db.execute(report_share_stmt(token))).scalar_one_or_none()
    if report is not None:
        return SuccessResponse(data={
            "type": "report",
            "title": report.title,
            "blocks": report.snapshot_blocks,
        })

    raise _share_not_found()
