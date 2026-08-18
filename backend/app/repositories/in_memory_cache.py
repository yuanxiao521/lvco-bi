"""In-memory cache repository for testing."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _CacheEntry:
    value: str
    expire_at: float | None = None

    @property
    def expired(self) -> bool:
        if self.expire_at is None:
            return False
        return time.time() > self.expire_at


class InMemoryCacheRepository:
    """纯内存缓存实现，用于单元测试。"""

    def __init__(self) -> None:
        self._store: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expired:
            del self._store[key]
            return None
        return entry.value

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        expire_at = (time.time() + ttl) if ttl else None
        self._store[key] = _CacheEntry(value=value, expire_at=expire_at)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def exists(self, key: str) -> bool:
        entry = self._store.get(key)
        if entry is None:
            return False
        if entry.expired:
            del self._store[key]
            return False
        return True

    def clear(self) -> None:
        """清空所有缓存（测试用）。"""
        self._store.clear()

    def keys(self) -> list[str]:
        """返回所有未过期的 key（测试用）。"""
        now = time.time()
        return [k for k, v in self._store.items() if v.expire_at is None or v.expire_at > now]
