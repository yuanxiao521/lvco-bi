"""DashboardScheduler - Dashboard 自动刷新调度器

负责按 cron 规则触发 Dashboard 的刷新：
- 5 段标准 cron 表达式（minute hour day month day_of_week）
- APScheduler BackgroundScheduler 集成
- 单例由 app.state 持有（参考 InsightScheduler 模式）

测试覆盖范围（test_dashboard_scheduler.py，Task 8）：
- _job_id：job_id 格式
- start / stop 幂等
- add_job 重复添加替换
- remove_job 不存在时 noop
- list_jobs 仅返回 dashboard:* 前缀的 job
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_CRON_PATTERN = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")
_JOB_PREFIX = "dashboard:"


def _job_id(dashboard_id: UUID | str) -> str:
    return f"{_JOB_PREFIX}{dashboard_id}"


def _parse_cron(cron: str) -> tuple[str, str, str, str, str]:
    cron = (cron or "").strip()
    if not _CRON_PATTERN.match(cron):
        raise ValueError(f"invalid_cron_expression: {cron!r}")
    parts = cron.split()
    if len(parts) != 5:
        raise ValueError(f"invalid_cron_expression: {cron!r}")
    minute, hour, day, month, day_of_week = parts
    return minute, hour, day, month, day_of_week


class DashboardScheduler:
    """Dashboard 自动刷新调度器（封装 APScheduler BackgroundScheduler）。

    由 main.py 在 startup 时实例化并 start，存到 app.state；
    shutdown 时调用 stop。
    """

    def __init__(self, scheduler: Any | None = None) -> None:
        self._started = False
        self._scheduler: Any = scheduler
        self._owns_scheduler = scheduler is None

    def start(self) -> None:
        if self._started:
            return
        if self._scheduler is None:
            from apscheduler.schedulers.background import BackgroundScheduler

            self._scheduler = BackgroundScheduler(daemon=True)
            self._owns_scheduler = True
        if not self._scheduler.running:
            self._scheduler.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=True)
            except Exception as e:
                logger.warning(f"dashboard_scheduler_shutdown_failed: {e}")
            if self._owns_scheduler:
                self._scheduler = None
        self._started = False

    def add_job(self, dashboard_id: UUID | str, cron: str) -> None:
        from apscheduler.triggers.cron import CronTrigger

        minute, hour, day, month, day_of_week = _parse_cron(cron)
        job_id = _job_id(dashboard_id)

        if not self._started:
            self.start()
        if self._scheduler is None:
            raise RuntimeError("dashboard_scheduler_not_started")

        self._scheduler.add_job(
            _execute_refresh,
            CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
            ),
            id=job_id,
            args=[str(dashboard_id)],
            replace_existing=True,
        )

    def remove_job(self, dashboard_id: UUID | str) -> None:
        if not self._started or self._scheduler is None:
            return
        job_id = _job_id(dashboard_id)
        try:
            self._scheduler.remove_job(job_id)
        except Exception as e:
            logger.debug(f"dashboard_remove_job_noop {job_id}: {e}")

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self._started or self._scheduler is None:
            return []
        result: list[dict[str, Any]] = []
        for job in self._scheduler.get_jobs():
            jid = job.id
            if not jid.startswith(_JOB_PREFIX):
                continue
            dashboard_id = jid[len(_JOB_PREFIX):]
            result.append(
                {
                    "dashboard_id": dashboard_id,
                    "next_run": (
                        job.next_run_time.isoformat() if job.next_run_time else None
                    ),
                }
            )
        return result


def _execute_refresh(dashboard_id_str: str) -> None:
    dashboard_id = UUID(dashboard_id_str)
    try:
        asyncio.run(_run_refresh(dashboard_id))
    except Exception as e:
        logger.exception(f"dashboard_refresh_failed {dashboard_id}: {e}")
        asyncio.run(_handle_refresh_failure(dashboard_id, e))


async def _run_refresh(dashboard_id: UUID) -> None:
    from app.api.deps import get_cache_repository
    from app.core.database import async_session_factory
    from app.repositories.unit_of_work import UnitOfWork
    from app.services.dashboard_service import DashboardService

    async with async_session_factory() as db:
        uow = UnitOfWork(db)
        cache_repo = get_cache_repository()
        service = DashboardService(
            dashboard_repo=uow.dashboard_repo,
            dashboard_chart_repo=uow.dashboard_chart_repo,
            db=db,
            cache_repo=cache_repo,
        )
        await service.refresh_dashboard_data(dashboard_id)
        await db.commit()

    async with async_session_factory() as db2:
        from sqlalchemy import update

        from app.models.dashboard import Dashboard

        await db2.execute(
            update(Dashboard)
            .where(Dashboard.id == dashboard_id)
            .values(last_refreshed_at=datetime.now(timezone.utc))
        )
        await db2.commit()


async def refresh_dashboard_and_notify(dashboard_id: UUID) -> int:
    """刷新指定仪表盘数据，并用 SSE 通知所有者数据已更新。

    供指标口径变更联动使用：先重新按最新口径查询并覆盖缓存，再推
    ``dashboard_updated`` 事件给仪表盘所有者，前端据此自动 refetch。

    Returns:
        成功投递的 SSE 订阅数（仪表盘无所有者时返回 0）。
    """
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.core.sse import sse_manager
    from app.models.dashboard import Dashboard

    await _run_refresh(dashboard_id)

    async with async_session_factory() as db:
        result = await db.execute(select(Dashboard.user_id).where(Dashboard.id == dashboard_id))
        owner = result.scalar_one_or_none()

    if owner is None:
        return 0
    return await sse_manager.publish(
        owner,
        "dashboard_updated",
        {"dashboardId": str(dashboard_id), "reason": "metric_updated"},
    )


async def _handle_refresh_failure(dashboard_id: UUID, error: Exception) -> None:
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.dashboard import Dashboard
    from app.models.notification import NotificationType
    from app.models.operation_log import OperationLog
    from app.services.notification_service import push_notification

    async with async_session_factory() as db:
        result = await db.execute(select(Dashboard).where(Dashboard.id == dashboard_id))
        dashboard = result.scalar_one_or_none()
        if dashboard is None:
            logger.warning(f"dashboard_not_found_for_notification {dashboard_id}")
            return

        error_msg = str(error)
        title = "仪表盘刷新失败"
        body = f"仪表盘 [{dashboard.title}] 刷新失败，原因：{error_msg}"

        await push_notification(
            user_id=dashboard.user_id,
            type_=NotificationType.system,
            title=title,
            body=body,
            resource_type="dashboard",
            resource_id=dashboard_id,
        )

        log_entry = OperationLog(
            user_id=dashboard.user_id,
            action="refresh_failed",
            resource_type="dashboard",
            resource_id=dashboard_id,
            method="SCHEDULER",
            path=f"/scheduler/dashboard/refresh/{dashboard_id}",
            status_code=0,
            duration_ms=0,
            extra={"detail": error_msg},
        )
        db.add(log_entry)
        await db.commit()