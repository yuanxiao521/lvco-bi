"""InsightScheduler 单元测试 - 覆盖纯逻辑部分（不依赖真实 DB）"""
import uuid
from datetime import time

import pytest

from app.models.insight_rule import ScheduleType
from app.services.insight_engine.scheduler import (
    InsightScheduler,
    _advisory_lock_key,
    _rule_job_id,
)


def test_rule_job_id_format():
    """job_id 应该是 insight_rule:<uuid> 格式"""
    rid = uuid.uuid4()
    assert _rule_job_id(rid) == f"insight_rule:{rid}"


def test_rule_job_id_accepts_str():
    """支持 str 输入（API 路径参数是 str）"""
    rid_str = "550e8400-e29b-41d4-a716-446655440000"
    assert _rule_job_id(rid_str) == f"insight_rule:{rid_str}"


def test_advisory_lock_key_is_stable():
    """同一 UUID 永远返回同一 key"""
    rid = uuid.uuid4()
    key1 = _advisory_lock_key(rid)
    key2 = _advisory_lock_key(rid)
    assert key1 == key2


def test_advisory_lock_key_is_signed_int64():
    """key 应在 PostgreSQL bigint 范围 [-(2^63), 2^63-1]"""
    for _ in range(100):
        rid = uuid.uuid4()
        key = _advisory_lock_key(rid)
        assert isinstance(key, int)
        assert -(2**63) <= key <= 2**63 - 1


def test_advisory_lock_key_differs_for_different_uuids():
    """不同 UUID 应生成不同 key（碰撞概率极低）"""
    rid1 = uuid.uuid4()
    rid2 = uuid.uuid4()
    assert _advisory_lock_key(rid1) != _advisory_lock_key(rid2)


def test_build_trigger_daily():
    """daily schedule 应生成每日 HH:MM 触发的 cron"""
    scheduler = InsightScheduler()
    trigger = scheduler._build_trigger(ScheduleType.daily, time(9, 30, 0))
    # CronTrigger 有 fields 属性，包含 day_of_week/hour/minute 等
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields.get("hour") == "9"
    assert fields.get("minute") == "30"
    # daily 应该每天都触发
    assert fields.get("day_of_week") == "*"


def test_build_trigger_weekly():
    """weekly schedule 应生成每周一 HH:MM 触发的 cron"""
    scheduler = InsightScheduler()
    trigger = scheduler._build_trigger(ScheduleType.weekly, time(8, 0, 0))
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields.get("hour") == "8"
    assert fields.get("minute") == "0"
    # weekly 应该只在周一触发（mon 或 0 取决于实现）
    assert fields.get("day_of_week") in ("mon", "0", "1")


@pytest.mark.anyio
async def test_reload_rule_when_not_started_is_noop():
    """scheduler 未启动时 reload_rule 不应抛异常"""
    scheduler = InsightScheduler()
    # 未调用 start()，应静默返回
    await scheduler.reload_rule(uuid.uuid4())


@pytest.mark.anyio
async def test_remove_rule_when_not_started_is_noop():
    """scheduler 未启动时 remove_rule 不应抛异常"""
    scheduler = InsightScheduler()
    await scheduler.remove_rule(uuid.uuid4())


@pytest.mark.anyio
async def test_shutdown_when_not_started_is_noop():
    """scheduler 未启动时 shutdown 不应抛异常"""
    scheduler = InsightScheduler()
    await scheduler.shutdown()
