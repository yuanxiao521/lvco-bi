import json
import structlog
from redis import Redis, ConnectionError as RedisConnectionError
from app.config import settings

logger = structlog.get_logger("cache")

class SimpleCache:
    """In-memory fallback cache (dict-based)."""
    def __init__(self):
        self._store: dict[str, str] = {}
    
    def get(self, key: str) -> str | None:
        value = self._store.get(f"lvco:{key}")
        logger.debug("fallback_cache_get", key=key, hit=value is not None)
        return value
    
    def set(self, key: str, value: str, ttl: int = 300):
        self._store[f"lvco:{key}"] = value
        logger.debug("fallback_cache_set", key=key, ttl=ttl, value_length=len(value))
    
    def delete(self, key: str):
        existed = f"lvco:{key}" in self._store
        self._store.pop(f"lvco:{key}", None)
        logger.debug("fallback_cache_delete", key=key, existed=existed)
    
    def exists(self, key: str) -> bool:
        exists = f"lvco:{key}" in self._store
        logger.debug("fallback_cache_exists", key=key, exists=exists)
        return exists

class CacheService:
    def __init__(self):
        self._redis: Redis | None = None
        self._fallback = SimpleCache()
        self._init_redis()
    
    def _init_redis(self):
        try:
            self._redis = Redis.from_url(settings.redis_url, socket_connect_timeout=3, decode_responses=True)
            self._redis.ping()
            logger.info("redis_connected", url=settings.redis_url)
        except (RedisConnectionError, Exception) as e:
            logger.warning("redis_unavailable_fallback", error=str(e), url=settings.redis_url)
            self._redis = None
    
    def get(self, key: str) -> str | None:
        full_key = f"lvco:{key}"
        if self._redis:
            try:
                value = self._redis.get(full_key)
                logger.debug("redis_get_success", key=key, hit=value is not None)
                return value
            except Exception as e:
                logger.warning("redis_get_failed", key=key, error=str(e))
        logger.debug("fallback_cache_get", key=key)
        return self._fallback.get(key)
    
    def set(self, key: str, value: str, ttl: int | None = None):
        full_key = f"lvco:{key}"
        expire = ttl if ttl is not None else settings.redis_ttl
        if self._redis:
            try:
                self._redis.setex(full_key, expire, value)
                logger.debug("redis_set_success", key=key, ttl=expire, value_length=len(value))
                return
            except Exception as e:
                logger.warning("redis_set_failed", key=key, error=str(e))
        logger.debug("fallback_cache_set", key=key, ttl=expire)
        self._fallback.set(key, value, expire)
    
    def delete(self, key: str):
        full_key = f"lvco:{key}"
        if self._redis:
            try:
                deleted = self._redis.delete(full_key)
                logger.debug("redis_delete_success", key=key, deleted=deleted)
                return
            except Exception as e:
                logger.warning("redis_delete_failed", key=key, error=str(e))
        logger.debug("fallback_cache_delete", key=key)
        self._fallback.delete(key)
    
    def exists(self, key: str) -> bool:
        full_key = f"lvco:{key}"
        if self._redis:
            try:
                exists = bool(self._redis.exists(full_key))
                logger.debug("redis_exists_success", key=key, exists=exists)
                return exists
            except Exception as e:
                logger.warning("redis_exists_failed", key=key, error=str(e))
        logger.debug("fallback_cache_exists", key=key)
        return self._fallback.exists(key)

# Module-level singleton
cache = CacheService()
