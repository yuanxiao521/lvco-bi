from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.services.dashboard_service import DashboardService

USER_ID = UUID("11111111-1111-1111-1111-111111111111")


def _make_dashboard_mock(user_id: UUID = USER_ID) -> MagicMock:
    d = MagicMock()
    d.id = uuid4()
    d.user_id = user_id
    d.refresh_cron = None
    d.refresh_enabled = False
    return d


@pytest.fixture
def mock_dashboard_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


@pytest.fixture
def mock_scheduler() -> MagicMock:
    return MagicMock()


@pytest.fixture
def service(
    mock_dashboard_repo: AsyncMock,
    mock_db: MagicMock,
    mock_scheduler: MagicMock,
) -> DashboardService:
    return DashboardService(
        dashboard_repo=mock_dashboard_repo,
        dashboard_chart_repo=AsyncMock(),
        db=mock_db,
        cache_repo=MagicMock(),
        dashboard_scheduler=mock_scheduler,
    )


class TestScheduleRefresh:
    @pytest.mark.asyncio
    async def test_enable_success(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_scheduler: MagicMock,
    ) -> None:
        dashboard = _make_dashboard_mock()
        mock_dashboard_repo.get_by_id.return_value = dashboard

        result = await service.schedule_refresh(
            dashboard_id=dashboard.id,
            user_id=USER_ID,
            cron="0 */5 * * *",
            enabled=True,
        )

        assert dashboard.refresh_cron == "0 */5 * * *"
        assert dashboard.refresh_enabled is True
        mock_scheduler.add_job.assert_called_once_with(dashboard.id, "0 */5 * * *")
        mock_scheduler.remove_job.assert_not_called()
        assert result == {
            "dashboard_id": str(dashboard.id),
            "refresh_enabled": True,
            "refresh_cron": "0 */5 * * *",
        }

    @pytest.mark.asyncio
    async def test_enable_invalid_cron_raises(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        dashboard = _make_dashboard_mock()
        mock_dashboard_repo.get_by_id.return_value = dashboard

        with pytest.raises(ValueError, match="invalid_cron_expression"):
            await service.schedule_refresh(
                dashboard_id=dashboard.id,
                user_id=USER_ID,
                cron="not-a-cron",
                enabled=True,
            )

    @pytest.mark.asyncio
    async def test_disable_removes_job(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_scheduler: MagicMock,
    ) -> None:
        dashboard = _make_dashboard_mock()
        dashboard.refresh_cron = "0 */5 * * *"
        dashboard.refresh_enabled = True
        mock_dashboard_repo.get_by_id.return_value = dashboard

        result = await service.schedule_refresh(
            dashboard_id=dashboard.id,
            user_id=USER_ID,
            cron="0 */5 * * *",
            enabled=False,
        )

        assert dashboard.refresh_cron is None
        assert dashboard.refresh_enabled is False
        mock_scheduler.remove_job.assert_called_once_with(dashboard.id)
        mock_scheduler.add_job.assert_not_called()
        assert result["refresh_enabled"] is False
        assert result["refresh_cron"] is None

    @pytest.mark.asyncio
    async def test_not_found_raises(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        mock_dashboard_repo.get_by_id.return_value = None

        with pytest.raises(ValueError, match="dashboard_not_found"):
            await service.schedule_refresh(
                dashboard_id=uuid4(),
                user_id=USER_ID,
                cron="0 */5 * * *",
                enabled=True,
            )

    @pytest.mark.asyncio
    async def test_permission_denied_raises(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        owner_id = UUID("22222222-2222-2222-2222-222222222222")
        dashboard = _make_dashboard_mock(user_id=owner_id)
        mock_dashboard_repo.get_by_id.return_value = dashboard

        with pytest.raises(PermissionError, match="permission_denied"):
            await service.schedule_refresh(
                dashboard_id=dashboard.id,
                user_id=USER_ID,
                cron="0 */5 * * *",
                enabled=True,
            )

    @pytest.mark.asyncio
    async def test_flush_and_commit_called(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_db: MagicMock,
    ) -> None:
        dashboard = _make_dashboard_mock()
        mock_dashboard_repo.get_by_id.return_value = dashboard

        await service.schedule_refresh(
            dashboard_id=dashboard.id,
            user_id=USER_ID,
            cron="0 9 * * 1",
            enabled=True,
        )

        mock_db.flush.assert_called_once()
        mock_db.commit.assert_called_once()