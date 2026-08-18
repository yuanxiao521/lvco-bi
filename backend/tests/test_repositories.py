"""Repository 层单元测试。"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.repositories.in_memory_cache import InMemoryCacheRepository
from app.repositories.protocols import CacheRepository
from app.repositories.fallback_cache import FallbackCacheRepository


# ── InMemoryCacheRepository ──────────────────────────────────────────────────


class TestInMemoryCacheRepository:
    """InMemoryCacheRepository 测试。"""

    def setup_method(self) -> None:
        self.cache = InMemoryCacheRepository()

    def test_get_returns_none_for_missing_key(self) -> None:
        assert self.cache.get("nonexistent") is None

    def test_set_and_get(self) -> None:
        self.cache.set("foo", "bar")
        assert self.cache.get("foo") == "bar"

    def test_delete(self) -> None:
        self.cache.set("foo", "bar")
        self.cache.delete("foo")
        assert self.cache.get("foo") is None

    def test_delete_nonexistent_key_is_noop(self) -> None:
        self.cache.delete("nonexistent")  # 不应抛异常

    def test_exists_true_for_existing_key(self) -> None:
        self.cache.set("foo", "bar")
        assert self.cache.exists("foo") is True

    def test_exists_false_for_missing_key(self) -> None:
        assert self.cache.exists("nonexistent") is False

    def test_ttl_expires(self) -> None:
        self.cache.set("foo", "bar", ttl=1)
        assert self.cache.get("foo") == "bar"
        time.sleep(1.1)
        assert self.cache.get("foo") is None

    def test_exists_returns_false_after_ttl(self) -> None:
        self.cache.set("foo", "bar", ttl=1)
        assert self.cache.exists("foo") is True
        time.sleep(1.1)
        assert self.cache.exists("foo") is False

    def test_clear(self) -> None:
        self.cache.set("a", "1")
        self.cache.set("b", "2")
        self.cache.clear()
        assert self.cache.keys() == []

    def test_keys_returns_non_expired(self) -> None:
        self.cache.set("a", "1")
        self.cache.set("b", "2", ttl=1)
        keys = self.cache.keys()
        assert "a" in keys
        assert "b" in keys
        time.sleep(1.1)
        keys = self.cache.keys()
        assert "a" in keys
        assert "b" not in keys

    def test_overwrite_value(self) -> None:
        self.cache.set("foo", "bar")
        self.cache.set("foo", "baz")
        assert self.cache.get("foo") == "baz"

    def test_set_without_ttl_has_no_expiry(self) -> None:
        self.cache.set("foo", "bar")
        # 没有 TTL，值应一直存在
        assert self.cache.get("foo") == "bar"
        assert self.cache.exists("foo") is True


# ── Protocol runtime_checkable ────────────────────────────────────────────────


class TestCacheRepositoryProtocol:
    """CacheRepository Protocol 测试。"""

    def test_in_memory_satisfies_protocol(self) -> None:
        cache = InMemoryCacheRepository()
        assert isinstance(cache, CacheRepository)

    def test_random_object_does_not_satisfy_protocol(self) -> None:
        class NotACache:
            pass
        assert not isinstance(NotACache(), CacheRepository)

    def test_partial_implementation_does_not_satisfy_protocol(self) -> None:
        class PartialCache:
            def get(self, key: str) -> str | None:
                return None
        assert not isinstance(PartialCache(), CacheRepository)


# ── FallbackCacheRepository ──────────────────────────────────────────────────


class TestFallbackCacheRepository:
    """FallbackCacheRepository 降级测试。"""

    def test_fallback_to_memory_when_redis_unavailable(self) -> None:
        """Redis 不可用时降级到内存缓存。"""
        with patch("app.repositories.fallback_cache.RedisCacheRepository") as MockRedis:
            mock_instance = MagicMock()
            mock_instance._redis = None  # 模拟 Redis 连接失败
            MockRedis.return_value = mock_instance

            repo = FallbackCacheRepository()
            assert repo._use_redis is False

            # 应通过内存缓存工作
            repo.set("foo", "bar")
            assert repo.get("foo") == "bar"
            assert repo.exists("foo") is True
            repo.delete("foo")
            assert repo.get("foo") is None

    def test_uses_redis_when_available(self) -> None:
        """Redis 可用时使用 Redis。"""
        with patch("app.repositories.fallback_cache.RedisCacheRepository") as MockRedis:
            mock_instance = MagicMock()
            mock_instance._redis = MagicMock()  # 模拟 Redis 已连接
            mock_instance.get.return_value = "redis_value"
            MockRedis.return_value = mock_instance

            repo = FallbackCacheRepository()
            assert repo._use_redis is True

            val = repo.get("foo")
            assert val == "redis_value"
            mock_instance.get.assert_called_once_with("foo")

    def test_fallback_satisfies_protocol(self) -> None:
        """FallbackCacheRepository 满足 CacheRepository Protocol。"""
        with patch("app.repositories.fallback_cache.RedisCacheRepository") as MockRedis:
            mock_instance = MagicMock()
            mock_instance._redis = None
            MockRedis.return_value = mock_instance

            repo = FallbackCacheRepository()
            assert isinstance(repo, CacheRepository)
