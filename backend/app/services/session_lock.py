"""会话级并发锁：同一会话同一时刻只允许一轮 Agent 在跑。

为什么不用进程内 asyncio.Lock：
- 锁带 TTL，进程崩溃/协程被取消时自动过期，不会永久死锁（自愈）；
- 将来把 uvicorn 扩成多 worker 时语义不变，不用改代码。

降级：Redis 不可用时退到进程内 NX（单进程下语义等价）；缓存层整个报错时
选择"放行"而不是拒绝——锁是防重复的优化，不该把用户挡在门外（宁可并发，不可不可用）。
"""
from __future__ import annotations

import logging
import uuid

from app.config import settings

logger = logging.getLogger("lvco.services.session_lock")

LOCK_KEY_PREFIX = "ai:session:lock:"


def _lock_key(session_id: object) -> str:
    return f"{LOCK_KEY_PREFIX}{session_id}"


def acquire_session_lock(session_id: object) -> str | None:
    """抢会话锁。

    Returns:
        抢到时返回 token（释放时凭它校验归属）；已被占用返回 None。
    """
    from app.api.deps import get_cache_repository

    token = uuid.uuid4().hex
    try:
        got = get_cache_repository().set_nx(
            _lock_key(session_id), token, ttl=settings.ai_session_lock_ttl
        )
    except Exception:
        logger.warning("session_lock_acquire_failed session=%s", session_id, exc_info=True)
        return token  # 缓存层异常 → 放行
    if got:
        logger.debug("session_lock_acquired session=%s", session_id)
        return token
    return None


def release_session_lock(session_id: object, token: str | None) -> None:
    """释放会话锁。

    校验 token 再删：若本轮的锁已因 TTL 过期而被别的请求抢走，不能误删它。
    """
    if not token:
        return
    from app.api.deps import get_cache_repository

    key = _lock_key(session_id)
    try:
        repo = get_cache_repository()
        if repo.get(key) == token:
            repo.delete(key)
    except Exception:
        logger.warning("session_lock_release_failed session=%s", session_id, exc_info=True)