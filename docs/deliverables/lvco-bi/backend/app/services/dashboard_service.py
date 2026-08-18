import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.chart_config import ChartConfig, ChartType
from app.models.dashboard import Dashboard
from app.models.dashboard_chart import DashboardChart
from app.models.datasource import DatasourceStatus
from app.services.query_engine import QueryEngineError, execute_chart_query


def _calc_pages(total: int, page_size: int) -> int:
    return math.ceil(total / page_size) if total > 0 else 0


def _make_share_token() -> str:
    return f"shr_{secrets.token_urlsafe(12)}"


class DashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, user_id: UUID, title: str, description: str | None = None) -> Dashboard:
        dashboard = Dashboard(
            user_id=user_id,
            title=title,
            description=description,
            layout=[],
        )
        self.db.add(dashboard)
        await self.db.flush()
        await self.db.refresh(dashboard)
        return dashboard

    async def list_dashboards(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        search: str | None,
    ) -> tuple[list[Dashboard], int]:
        from sqlalchemy.orm import selectinload
        q = select(Dashboard).where(Dashboard.user_id == user_id, Dashboard.deleted_at.is_(None))
        count_q = select(func.count()).select_from(Dashboard).where(Dashboard.user_id == user_id, Dashboard.deleted_at.is_(None))

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
        return items, total

    async def get_by_id(self, dashboard_id: UUID, user_id: UUID) -> Dashboard | None:
        result = await self.db.execute(
            select(Dashboard)
            .where(Dashboard.id == dashboard_id, Dashboard.user_id == user_id)
            .options(selectinload(Dashboard.dashboard_charts))
        )
        return result.scalar_one_or_none()

    async def update_layout(self, dashboard_id: UUID, user_id: UUID, layout: list[Any]) -> Dashboard | None:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return None
        dashboard.layout = layout
        await self.db.flush()
        await self.db.refresh(dashboard)
        return dashboard

    async def add_chart(
        self,
        dashboard_id: UUID,
        user_id: UUID,
        chart_config_id: UUID,
        title: str | None = None,
        position: dict | None = None,
    ) -> DashboardChart | None:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return None

        cc = await self.db.execute(
            select(ChartConfig).where(ChartConfig.id == chart_config_id)
        )
        if cc.scalar_one_or_none() is None:
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
        return dc

    async def remove_chart(self, dashboard_id: UUID, chart_id: UUID, user_id: UUID) -> bool:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return False

        result = await self.db.execute(
            select(DashboardChart).where(
                DashboardChart.id == chart_id,
                DashboardChart.dashboard_id == dashboard_id,
            )
        )
        dc = result.scalar_one_or_none()
        if dc is None:
            return False
        await self.db.delete(dc)
        await self.db.flush()
        return True

    async def delete(self, dashboard_id: UUID, user_id: UUID) -> bool:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return False
        # Soft delete
        dashboard.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True

    async def share(self, dashboard_id: UUID, user_id: UUID) -> Dashboard | None:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return None
        if dashboard.share_token is None:
            dashboard.share_token = _make_share_token()
        dashboard.is_public = True
        await self.db.flush()
        await self.db.refresh(dashboard)
        return dashboard

    async def get_dashboard_data(
        self,
        dashboard_id: UUID,
        user_id: UUID,
        use_cache: bool = True,
    ) -> dict | None:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return None

        from app.services.cache_service import cache
        cache_key = f"dashboard:{dashboard_id}:data"
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                return json.loads(cached)

        results: list[dict[str, Any]] = []
        for dc in dashboard.dashboard_charts:
            cc = await self.db.execute(
                select(ChartConfig).where(ChartConfig.id == dc.chart_config_id)
            )
            chart_config = cc.scalar_one_or_none()
            if chart_config is None:
                continue

            cfg = chart_config.query_config or {}
            # 兼容前端 camelCase 和 Python snake_case 两种 key 格式
            datasource_id_str = cfg.get("datasourceId") or cfg.get("datasource_id")
            if not datasource_id_str:
                results.append({
                    "chart_id": str(dc.id),
                    "title": dc.title,
                    "error": "数据源未配置",
                })
                continue

            try:
                ds_uuid = UUID(datasource_id_str)
                from app.schemas.query import ChartQueryConfig
                query_config = ChartQueryConfig.model_validate(cfg)
                query_result = await execute_chart_query(
                    datasource_id=ds_uuid,
                    config=query_config,
                    user_id=user_id,
                    db=self.db,
                )
                results.append({
                    "chart_id": str(dc.id),
                    "title": dc.title,
                    "chart_type": chart_config.chart_type.value,
                    "render_config": chart_config.render_config or {"renderer": "echarts", "palette": "default"},
                    "dimensions": query_config.dimensions,
                    "measures": [m.model_dump(mode="json") for m in query_config.measures],
                    "data": query_result.model_dump(mode="json"),
                })
            except (QueryEngineError, ValueError) as e:
                results.append({
                    "chart_id": str(dc.id),
                    "title": dc.title,
                    "error": str(e),
                })

        payload = {
            "dashboard_id": str(dashboard.id),
            "layout": dashboard.layout,
            "charts": results,
        }
        if use_cache:
            cache.set(cache_key, json.dumps(payload), ttl=dashboard.refresh_interval)
        return payload

    async def refresh(self, dashboard_id: UUID, user_id: UUID) -> bool:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return False
        from app.services.cache_service import cache
        cache.delete(f"dashboard:{dashboard_id}:data")
        return True