from __future__ import annotations

import pytest

from app.services.insight_engine.scheduler import InsightScheduler


@pytest.fixture
def scheduler() -> InsightScheduler:
    return InsightScheduler(interval_minutes=5)


class TestActivation:
    @pytest.mark.asyncio
    async def test_start_sets_started_flag(self, scheduler: InsightScheduler) -> None:
        scheduler.start()
        assert scheduler._started is True
        assert scheduler._scheduler is not None
        await scheduler.stop()
        assert scheduler._started is False
        assert scheduler._scheduler is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, scheduler: InsightScheduler) -> None:
        scheduler.start()
        first_scheduler = scheduler._scheduler
        scheduler.start()
        assert scheduler._started is True
        assert scheduler._scheduler is first_scheduler
        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_noop(self, scheduler: InsightScheduler) -> None:
        await scheduler.stop()
        assert scheduler._started is False
        assert scheduler._scheduler is None

    @pytest.mark.asyncio
    async def test_start_after_stop_restarts(self, scheduler: InsightScheduler) -> None:
        scheduler.start()
        await scheduler.stop()
        assert scheduler._started is False
        scheduler.start()
        assert scheduler._started is True
        await scheduler.stop()