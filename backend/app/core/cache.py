"""缓存仓库单例（下沉到 core，避免 services 层反向依赖 api 层）。

为什么放这里
------------
原先 `get_cache_repository()` 定义在 `app/api/deps.py`（依赖注入模块）。
但 `app/services/chat_task.py`（执行内核）也需要它——services 依赖 api 是**反向依赖**，
会让"执行内核"没法脱离 Web 层单独复用（worker 进程并不需要 FastAPI）。

放在 core 后：api 与 services 都从 core 取，依赖方向单向向下。
"""
from __future__ import annotations

from app.repositories.fallback_cache import FallbackCacheRepository
from app.repositories.in_memory_cache import InMemoryCacheRepository

_cache_repo: FallbackCacheRepository | None = None


def get_cache_repository() -> FallbackCacheRepository:
    """获取缓存仓库单例（优先 Redis，降级到内存）。"""
    global _cache_repo
    if _cache_repo is None:
        _cache_repo = FallbackCacheRepository()
    return _cache_repo


def get_test_cache_repository() -> InMemoryCacheRepository:
    """测试用：返回纯内存缓存。"""
    return InMemoryCacheRepository()


def reset_cache_repository() -> None:
    """测试用：清掉单例（便于注入替身）。"""
    global _cache_repo
    _cache_repo = None
