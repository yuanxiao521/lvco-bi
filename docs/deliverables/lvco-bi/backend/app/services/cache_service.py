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
        return self._store.get(f"lvco:{key}")
    
    def set(self, key: str, value: str, ttl: int = 300):
        self._store[f"lvco:{key}"] = value
    
    def delete(self, key: str):
        self._store.pop(f"lvco:{key}", None)
    
    def exists(self, key: str) -> bool:
        return f"lvco:{key}" in self._store

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
            logger.warning("redis_unavailable_fallback", error=str(e))
            self._redis = None
    
    def get(self, key: str) -> str | None:
        full_key = f"lvco:{key}"
        if self._redis:
            try:
                return self._redis.get(full_key)
            except Exception as e:
                logger.warning("redis_get_failed", key=key, error=str(e))
        return self._fallback.get(key)
    
    def set(self, key: str, value: str, ttl: int | None = None):
        full_key = f"lvco:{key}"
        expire = ttl if ttl is not None else settings.redis_ttl
        if self._redis:
            try:
                self._redis.setex(full_key, expire, value)
                return
            except Exception as e:
                logger.warning("redis_set_failed", key=key, error=str(e))
        self._fallback.set(key, value, expire)
    
    def delete(self, key: str):
        full_key = f"lvco:{key}"
        if self._redis:
            try:
                self._redis.delete(full_key)
                return
            except:
                pass
        self._fallback.delete(key)
    
    def exists(self, key: str) -> bool:
        full_key = f"lvco:{key}"
        if self._redis:
            try:
                return bool(self._redis.exists(full_key))
            except:
                pass
        return self._fallback.exists(key)

# Module-level singleton
cache = CacheService()
