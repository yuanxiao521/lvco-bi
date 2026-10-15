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

    # ── 跨进程共享状态专用（不写进程内存副本）──────────────────────────────

    def set_shared(self, key: str, value: str, ttl: int | None = None) -> None:
        """**跨进程共享键**专用写入：只写 Redis，不写本进程内存副本。

        适用：一个进程写、另一个进程读/删的键（如任务态 ai:chat:task:{sid}——
        API 进程写 running、worker 进程删）。若走通用 set 的「双写」，其他进程
        删除 Redis 后，本进程的内存副本会残留旧值，而 get 的 Redis-miss 回退
        会命中它——表现为「状态已结束但接口仍报 running」（幽灵数据）。

        Redis 不可用时的取舍：**不写任何副本**（含内存）。共享键的价值在于跨进程
        一致，内存副本是进程私有的——写它只会制造「本进程看得到、别的进程看不到」
        的假象，并在 Redis 恢复后重新引入幽灵态。因此宁可让状态"缺失"（前端不自动
        续收，手动 resume 仍可用），也不给出一份可能错的答案。
        注：FallbackCacheRepository 是进程级单例，_use_redis 在首次构造时确定。
        """
        if self._use_redis:
            self._redis.set(key, value, ttl)

    def delete_shared(self, key: str) -> None:
        """**跨进程共享键**专用删除：只删 Redis（见 set_shared 的说明）。"""
        if self._use_redis:
            self._redis.delete(key)

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
