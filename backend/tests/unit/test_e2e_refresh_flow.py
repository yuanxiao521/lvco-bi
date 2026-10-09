from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.services.dashboard_service import DashboardService

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
DASHBOARD_ID = UUID("22222222-2222-2222-2222-222222222222")
CHART_ID = UUID("33333333-3333-3333-3333-333333333333")
CHART_CFG_ID = UUID("44444444-4444-4444-4444-444444444444")


def _chart_config_mock() -> MagicMock:
    cc = MagicMock()
    cc.chart_type = MagicMock()
    cc.chart_type.value = "bar"
    cc.render_config = {"renderer": "echarts"}
    cc.query_config = {
        "datasourceId": "550e8400-e29b-41d4-a716-446655440000",
        "dimensions": ["month"],
        "measures": [{"field": "sales", "agg": "SUM"}],
    }
    return cc


def _dashboard_fixture(last_refreshed_at=None) -> MagicMock:
    dashboard = MagicMock()
    dashboard.id = DASHBOARD_ID
    dashboard.user_id = USER_ID
    dashboard.layout = []
    dashboard.last_refreshed_at = last_refreshed_at
    dc = MagicMock()
    dc.id = CHART_ID
    dc.chart_config_id = CHART_CFG_ID
    dc.title = "销售趋势"
    dashboard.dashboard_charts = [dc]
    return dashboard


class TestE2EManualRefresh:
    @pytest.mark.asyncio
    async def test_manual_refresh_updates_data_and_timestamp(self) -> None:
        dashboard = _dashboard_fixture(last_refreshed_at=None)

        # manual_refresh 走 dashboard_repo.get_by_id
        dashboard_repo = AsyncMock()
        dashboard_repo.get_by_id.return_value = dashboard

        # refresh_dashboard_data 走 self.db.execute / self.db.flush
        db = MagicMock()
        db_flush = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.flush = db_flush
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = dashboard
        db.execute = AsyncMock(return_value=session_result)

        dashboard_chart_repo = AsyncMock()
        dashboard_chart_repo.get_chart_config.return_value = _chart_config_mock()

        service = DashboardService(
            dashboard_repo=dashboard_repo,
            dashboard_chart_repo=dashboard_chart_repo,
            db=db,
            cache_repo=MagicMock(),
        )

        query_result = MagicMock()
        query_result.model_dump.return_value = {
            "rows": [{"month": "2024-01", "sales": 1000}]
        }
        with patch(
            "app.services.dashboard_service.execute_chart_query",
            new=AsyncMock(return_value=query_result),
        ) as mock_query:
            result = await service.manual_refresh(DASHBOARD_ID, USER_ID)

        mock_query.assert_awaited_once()
        assert result["dashboard_id"] == str(DASHBOARD_ID)
        assert len(result["charts"]) == 1
        assert result["charts"][0]["chartId"] == str(CHART_ID)
        assert result["charts"][0]["data"] == {
            "rows": [{"month": "2024-01", "sales": 1000}]
        }
        assert dashboard.last_refreshed_at is not None
        assert isinstance(dashboard.last_refreshed_at, datetime)
        db_flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_manual_refresh_returns_newer_data(self) -> None:
        dashboard = _dashboard_fixture(last_refreshed_at=None)

        dashboard_repo = AsyncMock()
        dashboard_repo.get_by_id.return_value = dashboard

        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.flush = AsyncMock()
        session_result = MagicMock()
        session_result.scalar_one_or_none.return_value = dashboard
        db.execute = AsyncMock(return_value=session_result)

        dashboard_chart_repo = AsyncMock()
        dashboard_chart_repo.get_chart_config.return_value = _chart_config_mock()

        service = DashboardService(
            dashboard_repo=dashboard_repo,
            dashboard_chart_repo=dashboard_chart_repo,
            db=db,
            cache_repo=MagicMock(),
        )

        def _query(**_kwargs):
            q = MagicMock()
            call_seq = _query.sequence
            _query.sequence += 1
            q.model_dump.return_value = {
                "rows": [{"month": "2024-01", "sales": 1000 * call_seq}]
            }
            return q

        _query.sequence = 1

        with patch(
            "app.services.dashboard_service.execute_chart_query",
            new=AsyncMock(side_effect=_query),
        ):
            first = await service.manual_refresh(DASHBOARD_ID, USER_ID)
            second = await service.manual_refresh(DASHBOARD_ID, USER_ID)

        first_sales = first["charts"][0]["data"]["rows"][0]["sales"]
        second_sales = second["charts"][0]["data"]["rows"][0]["sales"]
        assert first_sales == 1000
        assert second_sales == 2000
        assert second_sales > first_sales

    @pytest.mark.asyncio
    async def test_manual_refresh_not_found_raises(self) -> None:
        dashboard_repo = AsyncMock()
        dashboard_repo.get_by_id.return_value = None

        service = DashboardService(
            dashboard_repo=dashboard_repo,
            dashboard_chart_repo=AsyncMock(),
            db=MagicMock(),
            cache_repo=MagicMock(),
        )
        with patch(
            "app.services.dashboard_service.execute_chart_query",
            new=AsyncMock(),
        ) as mock_query:
            with pytest.raises(ValueError, match="dashboard_not_found"):
                await service.manual_refresh(DASHBOARD_ID, USER_ID)
        mock_query.assert_not_awaited()