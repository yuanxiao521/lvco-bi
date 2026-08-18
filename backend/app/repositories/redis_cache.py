"""Redis cache repository implementation."""
from __future__ import annotations

import logging

from redis import Redis, ConnectionError as RedisConnectionError

from app.config import settings

logger = logging.getLogger("lvco.repositories.redis_cache")


class RedisCacheRepository:
    """Redis 缓存实现。"""

    KEY_PREFIX = "lvco:"

    def __init__(self, redis_client: Redis | None = None) -> None:
        if redis_client is not None:
            self._redis = redis_client
        else:
            self._redis = self._connect()

    def _connect(self) -> Redis | None:
        try:
            r = Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                decode_responses=True,
            )
            r.ping()
            logger.info("redis_connected url=%s", settings.redis_url)
            return r
        except (RedisConnectionError, Exception) as e:
            logger.warning("redis_unavailable error=%s", e)
            return None

    def _full_key(self, key: str) -> str:
        return f"{self.KEY_PREFIX}{key}"

    def get(self, key: str) -> str | None:
        if not self._redis:
            return None
        try:
            return self._redis.get(self._full_key(key))
        except Exception as e:
            logger.warning("redis_get_failed key=%s error=%s", key, e)
            return None

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if not self._redis:
            return
        expire = ttl if ttl is not None else settings.redis_ttl
        try:
            self._redis.setex(self._full_key(key), expire, value)
        except Exception as e:
            logger.warning("redis_set_failed key=%s error=%s", key, e)

    def delete(self, key: str) -> None:
        if not self._redis:
            return
        try:
            self._redis.delete(self._full_key(key))
        except Exception as e:
            logger.warning("redis_delete_failed key=%s error=%s", key, e)

    def exists(self, key: str) -> bool:
        if not self._redis:
            return False
        try:
            return bool(self._redis.exists(self._full_key(key)))
        except Exception as e:
            logger.warning("redis_exists_failed key=%s error=%s", key, e)
            return False
