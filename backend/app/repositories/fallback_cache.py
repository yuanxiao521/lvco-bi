"""带 fallback 的缓存仓库：优先 Redis，降级到内存。"""
from __future__ import annotations

import logging

from app.repositories.in_memory_cache import InMemoryCacheRepository
from app.repositories.redis_cache import RedisCacheRepository

logger = logging.getLogger("lvco.repositories.fallback_cache")


class FallbackCacheRepository:
    """优先 Redis，连接失败时降级到内存缓存。"""

    def __init__(self) -> None:
        self._redis = RedisCacheRepository()
        self._memory = InMemoryCacheRepository()
        self._use_redis = self._redis._redis is not None

    def get(self, key: str) -> str | None:
        if self._use_redis:
            val = self._redis.get(key)
            if val is not None:
                return val
        return self._memory.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if self._use_redis:
            self._redis.set(key, value, ttl)
        self._memory.set(key, value, ttl)

    def delete(self, key: str) -> None:
        if self._use_redis:
            self._redis.delete(key)
        self._memory.delete(key)

    def exists(self, key: str) -> bool:
        if self._use_redis:
            return self._redis.exists(key)
        return self._memory.exists(key)
