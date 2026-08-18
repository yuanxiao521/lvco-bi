"""InsightScheduler - 基于 APScheduler 的智能洞察调度器

职责:
1. 应用启动时加载所有 enabled rules，注册 cron job
2. 规则 CRUD 时通过 reload_rule/remove_rule 增量更新
3. 调度入口 _execute_rule 内部用 PG advisory lock 防多实例重复触发
4. 调用 InsightRunner.run() 执行单条规则

设计要点:
- AsyncIOScheduler + SQLAlchemy AsyncSession
- CronTrigger: daily → every day at HH:MM; weekly → every Monday at HH:MM
- coalesce=True / max_instances=1 / misfire_grace_time 防止堆积
- PG advisory lock (pg_try_advisory_lock) 用 rule.id hash 作 key
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, time, timedelta
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.insight_rule import InsightRule, RunStatus, ScheduleType
from app.services.insight_engine.runner import InsightRunner

log = structlog.get_logger("insight_scheduler")

# misfire_grace_time: 错过执行窗口的容忍秒数（1 小时）
_MISFIRE_GRACE_SEC = 3600


def _rule_job_id(rule_id: uuid.UUID | str) -> str:
    return f"insight_rule:{rule_id}"


def _advisory_lock_key(rule_id: uuid.UUID) -> int:
    """把 rule UUID 哈希成 64-bit int 作为 PG advisory lock 的 key。

    PG advisory lock key 是 bigint，我们取 md5(uuid) 前 8 字节作为无符号整数，
    再映射到 [-(2^63), 2^63-1] 区间（用减 2^63 的方式）。
    """
    digest = hashlib.md5(rule_id.bytes).digest()
    raw = int.from_bytes(digest[:8], "big", signed=False)
    # 映射到 signed 64-bit 区间
    signed = raw - (1 << 63) if raw >= (1 << 63) else raw
    return signed


class InsightScheduler:
    """单例调度器，封装 APScheduler + DB 协同"""

    def __init__(self, runner: InsightRunner | None = None) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        self._runner = runner or InsightRunner()
        self._started = False

    # ============ Lifecycle ============
    async def start(self) -> None:
        """启动调度器并加载所有 enabled rules"""
        if self._started:
            log.warning("insight_scheduler_already_started")
            return

        self._scheduler = AsyncIOScheduler(
            timezone="UTC",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": _MISFIRE_GRACE_SEC,
            },
        )
        self._scheduler.start()
        self._started = True
        log.info("insight_scheduler_started")

        await self._load_all_rules()

    async def shutdown(self, wait: bool = True) -> None:
        """优雅关闭"""
        if not self._started or self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=wait)
            log.info("insight_scheduler_stopped")
        finally:
            self._started = False
            self._scheduler = None

    # ============ Public API ============
    async def reload_rule(self, rule_id: uuid.UUID | str) -> None:
        """规则更新后重新加载调度（创建/更新/paused→enabled/disabled→enabled 均走这里）"""
        if not self._started or self._scheduler is None:
            return
        rid = uuid.UUID(str(rule_id)) if not isinstance(rule_id, uuid.UUID) else rule_id

        # 先移除旧 job（无论是否存在）
        self._remove_job(rid)

        async with async_session_factory() as db:
            rule = await self._get_rule(db, rid)
            if rule is None or not rule.enabled:
                log.info("insight_scheduler_skip_reload", rule_id=str(rid), reason="disabled_or_missing")
                return
            self._add_job(rule)

    async def remove_rule(self, rule_id: uuid.UUID | str) -> None:
        """规则删除后从调度器移除"""
        if not self._started or self._scheduler is None:
            return
        rid = uuid.UUID(str(rule_id)) if not isinstance(rule_id, uuid.UUID) else rule_id
        self._remove_job(rid)

    # ============ Internal ============
    async def _load_all_rules(self) -> None:
        """启动时一次性加载所有 enabled rules"""
        if self._scheduler is None:
            return
        async with async_session_factory() as db:
            result = await db.execute(
                select(InsightRule).where(InsightRule.enabled.is_(True))
            )
            rules = list(result.scalars().all())

        loaded = 0
        for rule in rules:
            try:
                self._add_job(rule)
                loaded += 1
            except Exception as e:
                log.warning(
                    "insight_scheduler_load_rule_failed",
                    rule_id=str(rule.id),
                    error=str(e),
                )
        log.info("insight_scheduler_rules_loaded", total=len(rules), loaded=loaded)

    def _add_job(self, rule: InsightRule) -> None:
        """注册一条 rule 的 cron job"""
        if self._scheduler is None:
            return
        trigger = self._build_trigger(rule.schedule, rule.schedule_time)
        self._scheduler.add_job(
            self._execute_rule,
            trigger=trigger,
            args=[rule.id],
            id=_rule_job_id(rule.id),
            replace_existing=True,
        )
        log.info(
            "insight_scheduler_job_added",
            rule_id=str(rule.id),
            schedule=rule.schedule.value if rule.schedule else "daily",
            schedule_time=str(rule.schedule_time),
        )

    def _remove_job(self, rule_id: uuid.UUID) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_job(_rule_job_id(rule_id))
            log.info("insight_scheduler_job_removed", rule_id=str(rule_id))
        except Exception:
            # job 不存在，忽略
            pass

    def _build_trigger(self, schedule: ScheduleType, schedule_time: time) -> CronTrigger:
        """根据 schedule + schedule_time 构造 CronTrigger"""
        hour = schedule_time.hour
        minute = schedule_time.minute
        if schedule == ScheduleType.weekly:
            # 每周一执行
            return CronTrigger(day_of_week="mon", hour=hour, minute=minute, timezone="UTC")
        return CronTrigger(hour=hour, minute=minute, timezone="UTC")

    async def _get_rule(self, db: AsyncSession, rule_id: uuid.UUID) -> InsightRule | None:
        result = await db.execute(
            select(InsightRule).where(InsightRule.id == rule_id)
        )
        return result.scalar_one_or_none()

    async def _execute_rule(self, rule_id: uuid.UUID) -> None:
        """APScheduler 调度入口

        流程:
        1. 获取 PG advisory lock（非阻塞，失败则跳过——说明另一个实例正在执行）
        2. 查询 rule，若不存在/disabled 则跳过
        3. 计算 period_start/end
        4. 调用 InsightRunner.run()
        5. 释放 lock

        异常处理:
        - 所有异常都被捕获并 log，不让 scheduler 崩溃
        """
        lock_key = _advisory_lock_key(rule_id)
        lock_acquired = False

        try:
            # 1. 获取 advisory lock
            async with async_session_factory() as db:
                lock_result = await db.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
                )
                locked = lock_result.scalar()
                if not locked:
                    log.info(
                        "insight_scheduler_skip_locked",
                        rule_id=str(rule_id),
                        reason="another_instance_running",
                    )
                    return
                lock_acquired = True

            # 2. 查询 rule
            async with async_session_factory() as db:
                rule = await self._get_rule(db, rule_id)
                if rule is None:
                    log.info("insight_scheduler_skip_missing", rule_id=str(rule_id))
                    return
                if not rule.enabled:
                    log.info("insight_scheduler_skip_disabled", rule_id=str(rule_id))
                    return
                # 复制必要字段（rule 对象随 session 关闭会失效，转存简单数据）
                user_id = rule.user_id
                schedule_time = rule.schedule_time
                schedule = rule.schedule
                query_config = dict(rule.query_config or {})

            # 3. 计算 period
            now = datetime.utcnow()
            period_end = now
            days = int(query_config.get("time_range_days", 30) or 30)
            period_start = now - timedelta(days=days)

            # 4. 执行（用全新 session）
            log.info(
                "insight_scheduler_execute",
                rule_id=str(rule_id),
                period_start=period_start.isoformat(),
                period_end=period_end.isoformat(),
            )
            async with async_session_factory() as db:
                # 重新查 rule 给 runner 用
                rule = await self._get_rule(db, rule_id)
                if rule is None:
                    return
                await self._runner.run(db, rule, period_start, period_end)

        except Exception as e:
            log.exception(
                "insight_scheduler_execute_failed",
                rule_id=str(rule_id),
                error=str(e),
            )
        finally:
            # 5. 释放 advisory lock
            if lock_acquired:
                try:
                    async with async_session_factory() as db:
                        await db.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                        )
                        await db.commit()
                except Exception as unlock_err:
                    log.warning(
                        "insight_scheduler_unlock_failed",
                        rule_id=str(rule_id),
                        error=str(unlock_err),
                    )


# 单例
insight_scheduler = InsightScheduler()
