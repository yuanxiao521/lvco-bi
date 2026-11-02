"""连接健康检查调度器（连接级、非表级）。

为什么要有它：
轮换密码、库端改权限、防火墙策略变更、DB 重启——这些问题都不会主动通知 BI。
没有定时探测时，用户只能"点同步失败"才发现数据源挂了，仪表盘却在继续展示旧数据。
这里按连接（不是按表）做轻量探测，结果写进 db_connections.health_*，前端据此显示徽章。

实现选择：asyncio 后台任务而非 APScheduler —— 探测本身是 async（驱动在线程池里跑），
用 asyncio.sleep 循环最直接，也避免与 InsightScheduler/DashboardScheduler 的生命周期耦合。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.core.database import async_session_factory
from app.models.db_connection import DBConnection
from app.services.connection_service import ConnectionService

logger = logging.getLogger("lvco.connection_health")


class ConnectionHealthScheduler:
    def __init__(
        self,
        interval_minutes: int | None = None,
        max_per_cycle: int | None = None,
    ) -> None:
        self.interval_seconds = max(
            60, int((interval_minutes or settings.CONNECTION_HEALTH_INTERVAL_MINUTES) * 60)
        )
        self.max_per_cycle = int(max_per_cycle or settings.CONNECTION_HEALTH_MAX_PER_CYCLE)
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="connection-health-scheduler")
        logger.info(
            "connection_health_scheduler_started interval_s=%s max_per_cycle=%s",
            self.interval_seconds, self.max_per_cycle,
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None
        logger.info("connection_health_scheduler_stopped")

    async def _loop(self) -> None:
        # 启动后先等一个周期：避免与应用启动争抢连接池/让首个请求立刻变慢
        while not self._stopping:
            try:
                await asyncio.sleep(self.interval_seconds)
                if self._stopping:
                    break
                result = await self.run_once()
                if result["checked"]:
                    logger.info("connection_health_cycle_done %s", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("connection_health_cycle_failed error=%s", str(exc)[:200])

    # ── 单轮 ───────────────────────────────────────────────────────

    async def run_once(self) -> dict:
        """探测一轮"最久没检查/从未检查"的连接。"""
        stats = {"checked": 0, "ok": 0, "failed": 0}
        min_age = timedelta(minutes=int(settings.CONNECTION_HEALTH_MIN_AGE_MINUTES))
        cutoff = datetime.now(timezone.utc) - min_age
        async with async_session_factory() as db:
            result = await db.execute(
                select(DBConnection)
                .where(
                    (DBConnection.last_health_at.is_(None))
                    | (DBConnection.last_health_at < cutoff)
                )
                .order_by(DBConnection.last_health_at.asc().nullsfirst())
                .limit(self.max_per_cycle)
            )
            connections = list(result.scalars().all())
            if not connections:
                return stats
            service = ConnectionService(db)
            for connection in connections:
                try:
                    payload = await service.check_health(
                        connection.id, connection.user_id, force=True
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "connection_health_probe_error connection_id=%s error=%s",
                        connection.id, str(exc)[:160],
                    )
                    continue
                stats["checked"] += 1
                if payload and payload.get("status") == "ok":
                    stats["ok"] += 1
                else:
                    stats["failed"] += 1
            await db.commit()
        return stats
