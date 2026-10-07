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

    def set_nx(self, key: str, value: str, ttl: int | None = None) -> bool:
        """抢锁：Redis 可用时以 Redis 为准（多 worker 也互斥），掉线时降级内存 NX。

        与 get/set 的"两边都写"不同，NX 失败（别人持有）不能覆盖，因此只在抢到时才写内存副本。
        """
        if self._use_redis:
            got = self._redis.set_nx(key, value, ttl)
            if got is True:
                self._memory.set(key, value, ttl)
                return True
            if got is False:
                return False
            # got is None：Redis 掉线，降级内存
        return self._memory.set_nx(key, value, ttl)

    def delete(self, key: str) -> None:
        if self._use_redis:
            self._redis.delete(key)
        self._memory.delete(key)

    def delete_by_prefix(self, prefix: str) -> int:
        """按前缀清除两个层（Redis + 内存）。"""
        total = 0
        if self._use_redis:
            total += self._redis.delete_by_prefix(prefix)
        total += self._memory.delete_by_prefix(prefix)
        return total

    def exists(self, key: str) -> bool:
        if self._use_redis:
            return self._redis.exists(key)
        return self._memory.exists(key)
