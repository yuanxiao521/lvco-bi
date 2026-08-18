"""Dashboard Repository 实现。

封装仪表板相关的数据库操作，实现 DashboardRepository 协议。
"""
import logging
import secrets
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chart_config import ChartConfig
from app.models.dashboard import Dashboard
from app.models.dashboard_chart import DashboardChart

logger = logging.getLogger(__name__)


def _make_share_token() -> str:
    """生成分享 token。"""
    return f"shr_{secrets.token_urlsafe(12)}"


class SQLAlchemyDashboardRepository:
    """基于 SQLAlchemy 的仪表板仓库实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id: UUID,
        title: str,
        description: str | None = None,
    ) -> Dashboard:
        """创建仪表板。"""
        logger.info(f"创建仪表板 user_id={user_id} title={title}")
        dashboard = Dashboard(
            user_id=user_id,
            title=title,
            description=description,
            layout=[],
        )
        self.db.add(dashboard)
        await self.db.flush()
        await self.db.refresh(dashboard)
        logger.info(f"仪表板创建成功 dashboard_id={dashboard.id}")
        return dashboard

    async def list_dashboards(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        search: str | None = None,
    ) -> tuple[list[Dashboard], int]:
        """查询仪表板列表（分页）。"""
        logger.debug(f"查询仪表板列表 user_id={user_id} page={page} page_size={page_size} search={search}")
        
        q = select(Dashboard).where(Dashboard.user_id == user_id, Dashboard.deleted_at.is_(None))
        count_q = select(func.count()).select_from(Dashboard).where(
            Dashboard.user_id == user_id, Dashboard.deleted_at.is_(None)
        )

        if search:
            q = q.where(Dashboard.title.ilike(f"%{search}%"))
            count_q = count_q.where(Dashboard.title.ilike(f"%{search}%"))

        total = (await self.db.execute(count_q)).scalar() or 0

        q = (
            q.order_by(Dashboard.updated_at.desc().nullslast(), Dashboard.created_at.desc())
            .options(selectinload(Dashboard.dashboard_charts))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(q)
        items = list(result.scalars().all())
        
        logger.debug(f"仪表板列表查询完成 total={total} returned={len(items)}")
        return items, total

    async def get_by_id(self, dashboard_id: UUID, user_id: UUID) -> Dashboard | None:
        """根据 ID 查询仪表板。"""
        logger.debug(f"查询仪表板 dashboard_id={dashboard_id} user_id={user_id}")
        result = await self.db.execute(
            select(Dashboard)
            .where(Dashboard.id == dashboard_id, Dashboard.user_id == user_id)
            .options(selectinload(Dashboard.dashboard_charts))
        )
        dashboard = result.scalar_one_or_none()
        if dashboard:
            logger.debug(f"仪表板查询成功 dashboard_id={dashboard_id} title={dashboard.title}")
        else:
            logger.debug(f"仪表板未找到 dashboard_id={dashboard_id}")
        return dashboard

    async def update_layout(self, dashboard_id: UUID, user_id: UUID, layout: list[Any]) -> Dashboard | None:
        """更新仪表板布局。"""
        logger.info(f"更新仪表板布局 dashboard_id={dashboard_id} user_id={user_id} layout_count={len(layout)}")
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            logger.warning(f"更新仪表板布局失败：仪表板不存在 dashboard_id={dashboard_id}")
            return None
        dashboard.layout = layout
        await self.db.flush()
        await self.db.refresh(dashboard)
        logger.info(f"仪表板布局更新成功 dashboard_id={dashboard_id}")
        return dashboard

    async def delete(self, dashboard_id: UUID, user_id: UUID) -> bool:
        """软删除仪表板。"""
        logger.info(f"删除仪表板 dashboard_id={dashboard_id} user_id={user_id}")
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            logger.warning(f"删除仪表板失败：仪表板不存在 dashboard_id={dashboard_id}")
            return False
        # Soft delete
        dashboard.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        logger.info(f"仪表板删除成功 dashboard_id={dashboard_id}")
        return True

    async def share(self, dashboard_id: UUID, user_id: UUID) -> Dashboard | None:
        """生成仪表板分享链接。"""
        logger.info(f"生成仪表板分享链接 dashboard_id={dashboard_id} user_id={user_id}")
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            logger.warning(f"生成分享链接失败：仪表板不存在 dashboard_id={dashboard_id}")
            return None
        if dashboard.share_token is None:
            dashboard.share_token = _make_share_token()
        dashboard.is_public = True
        await self.db.flush()
        await self.db.refresh(dashboard)
        logger.info(f"仪表板分享链接生成成功 dashboard_id={dashboard_id} token={dashboard.share_token}")
        return dashboard


class SQLAlchemyDashboardChartRepository:
    """基于 SQLAlchemy 的仪表板图表仓库实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def add_chart(
        self,
        dashboard_id: UUID,
        chart_config_id: UUID,
        title: str | None = None,
        position: dict | None = None,
    ) -> DashboardChart | None:
        """添加图表到仪表板。"""
        logger.info(f"添加图表到仪表板 dashboard_id={dashboard_id} chart_config_id={chart_config_id}")
        
        # 检查图表配置是否存在
        cc_result = await self.db.execute(
            select(ChartConfig).where(ChartConfig.id == chart_config_id)
        )
        if cc_result.scalar_one_or_none() is None:
            logger.warning(f"添加图表失败：图表配置不存在 chart_config_id={chart_config_id}")
            return None

        dc = DashboardChart(
            dashboard_id=dashboard_id,
            chart_config_id=chart_config_id,
            title=title,
            position=position,
        )
        self.db.add(dc)
        await self.db.flush()
        await self.db.refresh(dc)
        logger.info(f"图表添加成功 dashboard_chart_id={dc.id}")
        return dc

    async def remove_chart(
        self,
        dashboard_id: UUID,
        chart_id: UUID,
        user_id: UUID,
    ) -> bool:
        """从仪表板移除图表。"""
        logger.info(f"从仪表板移除图表 dashboard_id={dashboard_id} chart_id={chart_id}")
        
        result = await self.db.execute(
            select(DashboardChart).where(
                DashboardChart.id == chart_id,
                DashboardChart.dashboard_id == dashboard_id,
            )
        )
        dc = result.scalar_one_or_none()
        if dc is None:
            logger.warning(f"移除图表失败：图表不存在 chart_id={chart_id}")
            return False
        self.db.delete(dc)
        await self.db.flush()
        logger.info(f"图表移除成功 chart_id={chart_id}")
        return True

    async def get_chart_config(self, chart_config_id: UUID) -> ChartConfig | None:
        """获取图表配置。"""
        logger.debug(f"获取图表配置 chart_config_id={chart_config_id}")
        result = await self.db.execute(
            select(ChartConfig).where(ChartConfig.id == chart_config_id)
        )
        config = result.scalar_one_or_none()
        if config:
            logger.debug(f"图表配置查询成功 chart_config_id={chart_config_id}")
        else:
            logger.debug(f"图表配置未找到 chart_config_id={chart_config_id}")
        return config
