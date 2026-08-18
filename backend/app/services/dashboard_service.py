import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard import Dashboard
from app.models.dashboard_chart import DashboardChart
from app.models.datasource import DatasourceStatus
from app.repositories.protocols import DashboardRepository, DashboardChartRepository
from app.services.query_engine import QueryEngineError, execute_chart_query


def _calc_pages(total: int, page_size: int) -> int:
    return math.ceil(total / page_size) if total > 0 else 0


class DashboardService:
    def __init__(
        self,
        dashboard_repo: DashboardRepository,
        dashboard_chart_repo: DashboardChartRepository,
        db: AsyncSession,
    ) -> None:
        self.dashboard_repo = dashboard_repo
        self.dashboard_chart_repo = dashboard_chart_repo
        self.db = db  # 保留 db 用于 execute_chart_query

    async def create(self, user_id: UUID, title: str, description: str | None = None) -> Dashboard:
        return await self.dashboard_repo.create(user_id, title, description)

    async def list_dashboards(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        search: str | None,
    ) -> tuple[list[Dashboard], int]:
        return await self.dashboard_repo.list_dashboards(user_id, page, page_size, search)

    async def get_by_id(self, dashboard_id: UUID, user_id: UUID) -> Dashboard | None:
        return await self.dashboard_repo.get_by_id(dashboard_id, user_id)

    async def update_layout(self, dashboard_id: UUID, user_id: UUID, layout: list[Any]) -> Dashboard | None:
        return await self.dashboard_repo.update_layout(dashboard_id, user_id, layout)

    async def add_chart(
        self,
        dashboard_id: UUID,
        user_id: UUID,
        chart_config_id: UUID,
        title: str | None = None,
        position: dict | None = None,
    ) -> DashboardChart | None:
        # 检查仪表板是否存在
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return None
        return await self.dashboard_chart_repo.add_chart(dashboard_id, chart_config_id, title, position)

    async def remove_chart(self, dashboard_id: UUID, chart_id: UUID, user_id: UUID) -> bool:
        # 检查仪表板是否存在
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return False
        return await self.dashboard_chart_repo.remove_chart(dashboard_id, chart_id, user_id)

    async def delete(self, dashboard_id: UUID, user_id: UUID) -> bool:
        return await self.dashboard_repo.delete(dashboard_id, user_id)

    async def share(self, dashboard_id: UUID, user_id: UUID) -> Dashboard | None:
        return await self.dashboard_repo.share(dashboard_id, user_id)

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
            # 通过 Repository 查询图表配置，避免 Service 直接访问数据库
            chart_config = await self.dashboard_chart_repo.get_chart_config(dc.chart_config_id)
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
                    "chartId": str(dc.id),
                    "title": dc.title,
                    "chartType": chart_config.chart_type.value,
                    "renderConfig": chart_config.render_config or {"renderer": "echarts", "palette": "default"},
                    "dimensions": query_config.dimensions,
                    "measures": [m.model_dump(mode="json") for m in query_config.measures],
                    "data": query_result.model_dump(mode="json"),
                })
            except (QueryEngineError, ValueError) as e:
                results.append({
                    "chartId": str(dc.id),
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