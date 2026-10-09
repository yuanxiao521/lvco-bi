import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dashboard import Dashboard
from app.models.dashboard_chart import DashboardChart
from app.models.datasource import DatasourceStatus
from app.repositories.protocols import DashboardRepository, DashboardChartRepository, CacheRepository
from app.services.query_engine import QueryEngineError, execute_chart_query


def _calc_pages(total: int, page_size: int) -> int:
    return math.ceil(total / page_size) if total > 0 else 0


class DashboardService:
    def __init__(
        self,
        dashboard_repo: DashboardRepository,
        dashboard_chart_repo: DashboardChartRepository,
        db: AsyncSession,
        cache_repo: CacheRepository | None = None,
        dashboard_scheduler: Any | None = None,
    ) -> None:
        self.dashboard_repo = dashboard_repo
        self.dashboard_chart_repo = dashboard_chart_repo
        self.db = db  # 保留 db 用于 execute_chart_query
        self.cache_repo = cache_repo
        self.dashboard_scheduler = dashboard_scheduler

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

        cache_key = f"dashboard:{dashboard_id}:data"
        if use_cache and self.cache_repo is not None:
            cached = self.cache_repo.get(cache_key)
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
                    cache_repo=self.cache_repo,
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
        if use_cache and self.cache_repo is not None:
            self.cache_repo.set(cache_key, json.dumps(payload), ttl=dashboard.refresh_interval)
        return payload

    async def refresh(self, dashboard_id: UUID, user_id: UUID) -> bool:
        dashboard = await self.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return False
        if self.cache_repo is not None:
            self.cache_repo.delete(f"dashboard:{dashboard_id}:data")
        return True

    async def refresh_dashboard_data(self, dashboard_id: UUID, db_session: AsyncSession | None = None) -> dict:
        session = db_session or self.db
        result = await session.execute(
            select(Dashboard)
            .where(Dashboard.id == dashboard_id)
            .options(selectinload(Dashboard.dashboard_charts))
        )
        dashboard = result.scalar_one_or_none()
        if dashboard is None:
            raise ValueError(f"dashboard_not_found: {dashboard_id}")

        results: list[dict[str, Any]] = []
        for dc in dashboard.dashboard_charts:
            chart_config = await self.dashboard_chart_repo.get_chart_config(dc.chart_config_id)
            if chart_config is None:
                continue

            cfg = chart_config.query_config or {}
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
                    user_id=dashboard.user_id,
                    db=session,
                    cache_repo=self.cache_repo,
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

        dashboard.last_refreshed_at = datetime.now(timezone.utc)
        await session.flush()

        return {
            "dashboard_id": str(dashboard.id),
            "layout": dashboard.layout,
            "charts": results,
        }

    async def schedule_refresh(self, dashboard_id: UUID, user_id: UUID, cron: str, enabled: bool) -> dict:
        dashboard = await self.dashboard_repo.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            raise ValueError(f"dashboard_not_found: {dashboard_id}")

        if dashboard.user_id != user_id:
            raise PermissionError("permission_denied")

        if enabled:
            if not re.match(r'^\S+\s+\S+\s+\S+\s+\S+\s+\S+$', cron):
                raise ValueError(f"invalid_cron_expression: {cron}")
            dashboard.refresh_cron = cron
            dashboard.refresh_enabled = True
            if self.dashboard_scheduler is not None:
                self.dashboard_scheduler.add_job(dashboard_id, cron)
        else:
            dashboard.refresh_cron = None
            dashboard.refresh_enabled = False
            if self.dashboard_scheduler is not None:
                self.dashboard_scheduler.remove_job(dashboard_id)

        await self.db.flush()
        await self.db.commit()

        return {
            "dashboard_id": str(dashboard.id),
            "refresh_enabled": enabled,
            "refresh_cron": cron if enabled else None,
        }

    async def manual_refresh(self, dashboard_id: UUID, user_id: UUID, async_mode: bool = False) -> dict:
        dashboard = await self.dashboard_repo.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            raise ValueError(f"dashboard_not_found: {dashboard_id}")

        if async_mode:
            asyncio.create_task(self._do_refresh(dashboard_id))
            return {"status": "accepted"}

        return await self.refresh_dashboard_data(dashboard_id)

    async def _do_refresh(self, dashboard_id: UUID) -> None:
        try:
            await self.refresh_dashboard_data(dashboard_id)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise