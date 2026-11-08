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

# P4-B：pop 时把整条挂起记录暂存到这里，供续跑分支取回 turn_id。
# 为什么用进程内小表而不是改返回值：pop_pending 的调用方（lead_agent._confirm_guard）
# 只认"目标字符串"，改返回类型会波及既有调用点；而 pop 与续跑在**同一进程**
# （in-process 在 API 进程、队列模式都在 worker 进程），进程内传递足够。
# 有界（64 条）：极端情况下宁可丢，也不无限增长。
_LAST_RESUMED_MAX = 64
_last_resumed: dict[str, dict] = {}


def _remember_resumed(session_id: object, payload: dict) -> None:
    try:
        if len(_last_resumed) >= _LAST_RESUMED_MAX:
            _last_resumed.clear()
        _last_resumed[str(session_id)] = dict(payload or {})
    except Exception:  # noqa: BLE001
        pass


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

    def set_pending(self, session_id: object, goal: str, *, turn_id: str | None = None) -> None:
        """ask_user 时写入挂起目标（用户原话）。

        P4-B：turn_id 一并挂起 —— HITL 确认续跑是新请求（新 trace/turn），
        续跑分支用 take_resumed_turn_id 取回它，让同一轮的记录串在原 turn_id 下。
        """
        if not session_id or not (goal or "").strip():
            return
        payload: dict[str, str] = {"goal": goal}
        if turn_id:
            payload["turn_id"] = str(turn_id)
        try:
            _repo().set(
                self._key(session_id),
                json.dumps(payload, ensure_ascii=False),
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
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                # P4-B：暂存整条记录（含 turn_id），供续跑分支取回
                _remember_resumed(session_id, payload)
                return str(payload.get("goal") or "") or None
        except Exception:  # noqa: BLE001
            logger.warning("lead_state_pop_failed session=%s", session_id, exc_info=True)
        return None

    def take_resumed_turn_id(self, session_id: object) -> str | None:
        """取回上一次 pop 的挂起记录里的 turn_id（一次性；没有则 None）。

        只在 HITL 续跑分支调用：走到那里说明这次请求就是"确认后继续上轮"。
        """
        if not session_id:
            return None
        try:
            payload = _last_resumed.pop(str(session_id), None) or {}
            value = payload.get("turn_id")
            return str(value) if value else None
        except Exception:  # noqa: BLE001
            return None


lead_session_state = LeadSessionState()
