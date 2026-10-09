from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.dashboard_scheduler import (
    DashboardScheduler,
    _job_id,
    _parse_cron,
)


# ── fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_apscheduler() -> MagicMock:
    """注入的 Mock BackgroundScheduler，避免真实启动调度线程。"""
    scheduler = MagicMock()
    scheduler.running = False
    return scheduler


# ── _job_id / _parse_cron 纯函数 ────────────────────────────────────────────

class TestCronValidation:
    def test_job_id_format(self):
        dashboard_id = uuid4()
        assert _job_id(dashboard_id) == f"dashboard:{dashboard_id}"

    def test_job_id_accepts_str(self):
        dashboard_id = "550e8400-e29b-41d4-a716-446655440000"
        assert _job_id(dashboard_id) == f"dashboard:{dashboard_id}"

    def test_parse_cron_valid_returns_five_fields(self):
        parsed = _parse_cron("0 */5 * * *")
        assert parsed == ("0", "*/5", "*", "*", "*")

    def test_parse_cron_whitespace_stripped(self):
        parsed = _parse_cron("  " + "0 9 * * 1" + "  ")
        assert parsed == ("0", "9", "*", "*", "1")

    def test_parse_cron_empty_raises(self):
        with pytest.raises(ValueError, match="invalid_cron_expression"):
            _parse_cron("")

    def test_parse_cron_spaces_only_raises(self):
        with pytest.raises(ValueError, match="invalid_cron_expression"):
            _parse_cron("     ")

    def test_parse_cron_missing_field_raises(self):
        with pytest.raises(ValueError, match="invalid_cron_expression"):
            _parse_cron("0 */5 * *")

    def test_parse_cron_extra_field_raises(self):
        with pytest.raises(ValueError, match="invalid_cron_expression"):
            _parse_cron("0 */5 * * * extra")


# ── start / stop ────────────────────────────────────────────────────────────

class TestStartStop:
    def test_start_with_injected_scheduler_starts_it(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        assert scheduler._started is True
        assert scheduler._owns_scheduler is False
        mock_apscheduler.start.assert_called_once()

    def test_start_idempotent(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        scheduler.start()
        assert scheduler._started is True
        mock_apscheduler.start.assert_called_once()

    def test_stop_does_not_shutdown_when_not_started(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.stop()
        mock_apscheduler.shutdown.assert_not_called()

    def test_stop_shutdowns_started_scheduler(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        scheduler.stop()
        assert scheduler._started is False
        mock_apscheduler.shutdown.assert_called_once()

    def test_stop_idempotent(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        scheduler.stop()
        scheduler.stop()
        mock_apscheduler.shutdown.assert_called_once()


# ── add_job / remove_job / list_jobs ────────────────────────────────────────

class TestJobManagement:
    def test_add_job_valid_cron(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        dashboard_id = uuid4()
        scheduler.add_job(dashboard_id, "0 */5 * * *")

        mock_apscheduler.add_job.assert_called_once()
        kwargs = mock_apscheduler.add_job.call_args.kwargs
        assert kwargs["id"] == f"dashboard:{dashboard_id}"
        assert kwargs["args"] == [str(dashboard_id)]
        assert kwargs["replace_existing"] is True

    def test_add_job_invalid_cron_raises(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        with pytest.raises(ValueError, match="invalid_cron_expression"):
            scheduler.add_job(uuid4(), "not-a-cron")
        mock_apscheduler.add_job.assert_not_called()

    def test_add_job_always_replace_existing(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.add_job(uuid4(), "0 * * * *")
        scheduler.add_job(uuid4(), "30 * * * *")
        for call in mock_apscheduler.add_job.call_args_list:
            assert call.kwargs["replace_existing"] is True

    def test_remove_job_calls_scheduler(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        dashboard_id = uuid4()
        scheduler.remove_job(dashboard_id)
        mock_apscheduler.remove_job.assert_called_once_with(f"dashboard:{dashboard_id}")

    def test_remove_job_when_not_started_is_noop(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.remove_job(uuid4())
        mock_apscheduler.remove_job.assert_not_called()

    def test_remove_job_idempotent_on_unknown_job(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        mock_apscheduler.remove_job.side_effect = KeyError("job not found")
        dashboard_id = uuid4()
        scheduler.remove_job(dashboard_id)
        scheduler.remove_job(dashboard_id)
        assert mock_apscheduler.remove_job.call_count == 2

    def test_remove_job_idempotent_when_removed_twice(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        dashboard_id = uuid4()
        scheduler.remove_job(dashboard_id)
        scheduler.remove_job(dashboard_id)
        assert mock_apscheduler.remove_job.call_count == 2

    def test_list_jobs_only_dashboard_prefix(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        scheduler.start()
        job_a = MagicMock()
        job_a.id = f"dashboard:{uuid4()}"
        job_a.next_run_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        job_other = MagicMock()
        job_other.id = f"insight_rule:{uuid4()}"
        job_other.next_run_time = None
        mock_apscheduler.get_jobs.return_value = [job_a, job_other]

        jobs = scheduler.list_jobs()

        assert len(jobs) == 1
        assert jobs[0]["dashboard_id"] == job_a.id[len("dashboard:"):]
        assert jobs[0]["next_run"] == job_a.next_run_time.isoformat()

    def test_list_jobs_not_started_returns_empty(self, mock_apscheduler: MagicMock):
        scheduler = DashboardScheduler(scheduler=mock_apscheduler)
        assert scheduler.list_jobs() == []
        mock_apscheduler.get_jobs.assert_not_called()