"""会话级并发锁测试（SET NX 互斥 / 校验 token 释放 / TTL 自愈 / 降级）。"""
from __future__ import annotations

import time

import pytest

from app.api import deps
from app.config import settings
from app.repositories.in_memory_cache import InMemoryCacheRepository
from app.services.session_lock import acquire_session_lock, release_session_lock


@pytest.fixture()
def memory_cache(monkeypatch) -> InMemoryCacheRepository:
    """把 API 层缓存单例换成纯内存实现（不依赖真实 Redis）。"""
    cache = InMemoryCacheRepository()
    monkeypatch.setattr(deps, "_cache_repo", cache)
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

    monkeypatch.setattr(deps, "_cache_repo", BrokenRepo())
    token = acquire_session_lock("s1")
    assert token
    release_session_lock("s1", token)  # 不应抛异常