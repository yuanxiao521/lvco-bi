"""Lead 主管会话挂起状态（AWAITING_CONFIRM 槽）。

设计要点（详见《主管状态机_设计方案_v2》）：
- 只持久化这一个状态：EXECUTING 由 session_lock 推导、IDLE 缺省即得，
  能推导的状态不存储，避免与锁形成双写不一致。
- Redis 存 JSON（{"goal": 用户原话}），TTL 对齐 ai_session_lock_ttl——
  反问后用户长时间不回应，槽自动过期，无需清理任务。
- fail-open：Redis 异常时按"无挂起"处理（记日志放行），与 session_lock 的降级哲学一致。
"""

import json
import logging
from enum import Enum

from app.config import settings

logger = logging.getLogger("lvco.lead.state")

KEY_PREFIX = "lvco:sess_pend:"


class LeadPhase(str, Enum):
    """主管会话状态（粗粒度，只管会话级；任务内细阶段走 SSE progress + 观测层）。

    三个状态、只持久化一个——能从权威事实源推导的不存储：
    - EXECUTING       由 session_lock 推导（锁即状态，避免双写不一致）
    - IDLE            缺省态（前两者都不成立）
    - AWAITING_CONFIRM 挂起槽（Redis，唯一落存储的状态）
    """

    IDLE = "idle"
    EXECUTING = "executing"
    AWAITING_CONFIRM = "awaiting_confirm"


def read_phase(session_id: object) -> LeadPhase:
    """当前会话状态（纯推导，不新增任何存储）。

    注意顺序：EXECUTING 优先于 AWAITING_CONFIRM——锁先被抢走时，
    "在跑"是更强的事实，此时不应表现为"等确认"。
    """
    if session_id:
        from app.services.session_lock import session_lock_held

        if session_lock_held(session_id):
            return LeadPhase.EXECUTING
        if pending_exists(session_id):
            return LeadPhase.AWAITING_CONFIRM
    return LeadPhase.IDLE


def pending_exists(session_id: object) -> bool:
    try:
        if not session_id:
            return False
        repo = _repo()
        return bool(repo.exists(f"{KEY_PREFIX}{session_id}"))
    except Exception:  # noqa: BLE001
        logger.warning("lead_state_probe_failed session=%s", session_id, exc_info=True)
        return False


def _repo():
    from app.api.deps import get_cache_repository

    return get_cache_repository()


class LeadSessionState:
    """按 session_id 存取"反问挂起目标"。pop 即消费（一次性，防跨轮误触发）。

    repo 获取统一走模块级 _repo()（单一事实源）——若这里再包一层实例方法，
    会出现两条获取路径，mock/替换时必然漏一边。
    """

    def _key(self, session_id: object) -> str:
        return f"{KEY_PREFIX}{session_id}"

    def _ttl(self) -> int:
        return int(getattr(settings, "ai_session_lock_ttl", 600))

    def set_pending(self, session_id: object, goal: str) -> None:
        """ask_user 时写入挂起目标（用户原话）。"""
        if not session_id or not (goal or "").strip():
            return
        try:
            _repo().set(
                self._key(session_id),
                json.dumps({"goal": goal}, ensure_ascii=False),
                ttl=self._ttl(),
            )
        except Exception:  # noqa: BLE001
            logger.warning("lead_state_set_failed session=%s", session_id, exc_info=True)

    def pop_pending(self, session_id: object) -> str | None:
        """取走挂起目标（读即删，一次性）。无槽 / 过期 / Redis 异常 → None。"""
        if not session_id:
            return None
        try:
            raw = _repo().get(self._key(session_id))
            if raw:
                _repo().delete(self._key(session_id))
                return str(json.loads(raw).get("goal") or "") or None
        except Exception:  # noqa: BLE001
            logger.warning("lead_state_pop_failed session=%s", session_id, exc_info=True)
        return None


lead_session_state = LeadSessionState()
