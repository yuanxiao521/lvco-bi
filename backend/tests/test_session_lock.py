"""会话级并发锁测试（SET NX 互斥 / 校验 token 释放 / TTL 自愈 / 降级）。"""
from __future__ import annotations

import time

import pytest

from app.config import settings
from app.core import cache as cache_module
from app.repositories.in_memory_cache import InMemoryCacheRepository
from app.services.session_lock import (
    acquire_session_lock,
    force_release_session_lock,
    release_session_lock,
    session_task_running,
)


@pytest.fixture()
def memory_cache(monkeypatch) -> InMemoryCacheRepository:
    """把缓存单例换成纯内存实现（不依赖真实 Redis）。

    单例已从 app/api/deps.py 下沉到 app/core/cache.py（services 层也要用，
    避免 services → api 反向依赖），因此这里 patch 的是 core 模块的私有单例。
    """
    cache = InMemoryCacheRepository()
    monkeypatch.setattr(cache_module, "_cache_repo", cache)
    return cache


def test_second_acquire_is_rejected(memory_cache) -> None:
    """同一会话同时只允许一轮：第二次抢锁失败。"""
    assert acquire_session_lock("s1")
    assert acquire_session_lock("s1") is None


def test_different_sessions_do_not_block_each_other(memory_cache) -> None:
    assert acquire_session_lock("s1")
    assert acquire_session_lock("s2")


def test_release_allows_reacquire(memory_cache) -> None:
    token = acquire_session_lock("s1")
    release_session_lock("s1", token)
    assert acquire_session_lock("s1")


def test_release_with_wrong_token_keeps_lock(memory_cache) -> None:
    """释放要校验 token：TTL 过期后别人抢到的锁不能被误删。"""
    acquire_session_lock("s1")
    release_session_lock("s1", "not-the-owner")
    assert acquire_session_lock("s1") is None


def test_lock_self_heals_after_ttl(memory_cache, monkeypatch) -> None:
    """没人释放（进程崩溃/协程被取消）时，TTL 兜底自动解锁。"""
    monkeypatch.setattr(settings, "ai_session_lock_ttl", 1)
    assert acquire_session_lock("s1")
    time.sleep(1.1)
    assert acquire_session_lock("s1")


def test_cache_failure_fails_open(monkeypatch) -> None:
    """缓存层异常时放行：锁是防重复的优化，不该把用户挡在门外。"""

    class BrokenRepo:
        def set_nx(self, *args, **kwargs):
            raise RuntimeError("cache down")

        def get(self, key):
            raise RuntimeError("cache down")

        def delete(self, key):
            raise RuntimeError("cache down")

    monkeypatch.setattr(cache_module, "_cache_repo", BrokenRepo())
    token = acquire_session_lock("s1")
    assert token
    release_session_lock("s1", token)  # 不应抛异常

# ── 孤儿锁自愈：断流/崩溃后锁未释放 → 不该把用户永久挡住 ──────────────

def _write_task_state(cache, sid: str, status: str, started_at: str | None = None, canvas: bool = False):
    import json

    payload = {"status": status}
    if started_at:
        payload["started_at"] = started_at
    cache.set(f"ai:{'canvas' if canvas else 'chat'}:task:{sid}", json.dumps(payload), ttl=600)


def test_task_not_running_when_no_state(memory_cache) -> None:
    """没有任务态记录 → 任务没在跑（可安全清孤儿锁）。"""
    assert session_task_running("s1") is False


def test_task_running_reads_cross_process_state(memory_cache) -> None:
    """running 视为在跑；completed 视为结束（锁应被清）。"""
    _write_task_state(memory_cache, "s1", "running")
    assert session_task_running("s1") is True
    _write_task_state(memory_cache, "s1", "completed")
    assert session_task_running("s1") is False


def test_task_stale_running_treated_as_not_running(memory_cache, monkeypatch) -> None:
    """running 但 started_at 已超阈值（worker 挂掉，Redis 状态永不更新）→ 视为没在跑。"""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "lead_task_stale_seconds", 60)
    old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    _write_task_state(memory_cache, "s1", "running", started_at=old, canvas=True)
    assert session_task_running("s1", canvas=True) is False

    fresh = datetime.now(timezone.utc).isoformat()
    _write_task_state(memory_cache, "s2", "running", started_at=fresh, canvas=True)
    assert session_task_running("s2", canvas=True) is True


def test_force_release_then_acquire(memory_cache) -> None:
    """孤儿锁强制清理后可立即重新抢到（用户不再被挡）。"""
    assert acquire_session_lock("s1")
    assert acquire_session_lock("s1") is None
    force_release_session_lock("s1")
    assert acquire_session_lock("s1")