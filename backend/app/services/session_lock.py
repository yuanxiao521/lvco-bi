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


def session_lock_held(session_id: object) -> bool:
    """查询会话锁是否被占用（供状态机推导 EXECUTING，不新增存储）。

    fail-open：查询异常按"未占用"处理——状态机只用于观测/守卫辅助，
    误判 IDLE 的代价远小于把用户挡在门外。
    """
    from app.api.deps import get_cache_repository

    try:
        return bool(get_cache_repository().exists(_lock_key(session_id)))
    except Exception:
        logger.warning("session_lock_probe_failed session=%s", session_id, exc_info=True)
        return False


def session_task_running(session_id: object, *, canvas: bool = False) -> bool:
    """任务是否真的在跑（跨进程真相：Redis 任务态）。

    锁本身不能证明任务在跑——进程崩溃/断流/任务被取消时锁还在（TTL 内），
    表现就是「上一轮还在处理中」挡住用户所有新消息（实测投诉点）。
    队列模式下任务跑在 worker 进程，本进程 registry 看不到，必须读 Redis。
    """
    from app.api.deps import get_cache_repository

    key = f"ai:{'canvas' if canvas else 'chat'}:task:{session_id}"
    try:
        raw = get_cache_repository().get(key)
    except Exception:
        logger.warning("session_task_probe_failed session=%s", session_id, exc_info=True)
        return True  # 探测失败时保守认为在跑，不误清锁
    if not raw:
        return False
    try:
        import json as _json
        from datetime import datetime, timezone

        data = _json.loads(raw) if isinstance(raw, str) else raw
        data = data or {}
        if str(data.get("status") or "") != "running":
            return False
        # 陈旧判定：worker 挂掉时 Redis 里的 running 永远不会被清，
        # 只看 status 会让用户被永久挡住 → 超阈值视为"已不在跑"。
        started = data.get("started_at")
        if isinstance(started, str) and started:
            try:
                ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                if age > int(getattr(settings, "lead_task_stale_seconds", 900)):
                    logger.warning("session_task_stale session=%s age=%.0fs", session_id, age)
                    return False
            except Exception:
                pass
        return True
    except Exception:
        return False


def force_release_session_lock(session_id: object) -> None:
    """强制清锁（仅在确认没有任务在跑时调用）：孤儿锁自愈用。"""
    from app.api.deps import get_cache_repository

    try:
        get_cache_repository().delete(_lock_key(session_id))
        logger.warning("session_lock_force_released session=%s", session_id)
    except Exception:
        logger.warning("session_lock_force_release_failed session=%s", session_id, exc_info=True)


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