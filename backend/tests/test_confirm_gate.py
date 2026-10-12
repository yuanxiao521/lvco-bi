# -*- coding: utf-8 -*-
"""主管状态机 + HITL 确认闸守卫。

这条链路横跨五处（挂起槽 / 状态机 / 决策守卫 / SSE 事件 / 前端卡片），
任何一处被误删或改坏，症状都是**静默的**——用户只是"点了确认没反应"或
"该弹的卡片不弹了"，不会报错。所以这里把它固化成断言。

链路回顾：
    主管 ask_user(ask_kind=confirm)
      → 挂起槽写入本轮目标（Redis，跨请求存活）
      → SSE 发 confirm_request → 前端渲染 amber 确认卡片
      → 用户点「确认执行」→ 下一请求带 ui_action={"type":"confirm"}
      → _confirm_guard **在决策之前**短路：直接恢复上轮目标派发
        （不调 merged LLM —— 这是"零猜测"的关键：状态迁移由显式事件驱动）
"""

from __future__ import annotations

import pytest

import app.services.lead_session_state as lss
from app.services.agents.lead.lead_agent import LeadAgent, LeadContext
from app.services.lead_session_state import LeadPhase, lead_session_state, read_phase


class _FakeRepo:
    """内存版挂起槽存储（不依赖 Redis，语义与生产实现一致：get/set/delete/exists）。"""

    def __init__(self) -> None:
        self.store: dict = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)

    def exists(self, key) -> bool:
        return key in self.store


@pytest.fixture
def fake_repo(monkeypatch) -> _FakeRepo:
    repo = _FakeRepo()
    # 单一获取路径（lead_session_state._repo）——patch 一处即全链路生效
    monkeypatch.setattr(lss, "_repo", lambda: repo)
    return repo


def _ctx(session_id: str = "sess-1", *, ui_action=None, entry: str = "canvas") -> LeadContext:
    return LeadContext(user_id="u1", session_id=session_id, entry=entry, ui_action=ui_action)


# ── 挂起槽：跨请求存活 + 一次性消费 ───────────────────────────────────


def test_pending_slot_is_one_shot(fake_repo):
    """pop 即消费：同一槽只能被取走一次（防跨轮误触发重复执行）。"""
    lead_session_state.set_pending("s1", "清空画布")
    assert lead_session_state.pop_pending("s1") == "清空画布"
    assert lead_session_state.pop_pending("s1") is None, "槽应一次性，不能重复取"


# ── 状态机：三态纯推导 ──────────────────────────────────────────────


def test_lead_phase_derivation(fake_repo, monkeypatch):
    """LeadPhase 三态：锁在 → EXECUTING（优先）；槽在 → AWAITING_CONFIRM；都无 → IDLE。"""
    import app.services.session_lock as sl

    monkeypatch.setattr(sl, "session_lock_held", lambda sid: fake_repo.exists(f"{sl.LOCK_KEY_PREFIX}{sid}"))

    assert read_phase("s9") is LeadPhase.IDLE

    lead_session_state.set_pending("s9", "清空画布")
    assert read_phase("s9") is LeadPhase.AWAITING_CONFIRM, "有挂起目标 = 等确认"

    fake_repo.set(f"{sl.LOCK_KEY_PREFIX}s9", "token")
    assert read_phase("s9") is LeadPhase.EXECUTING, "在跑是更强的事实，优先于等确认"


def test_lead_phase_fail_open_on_redis_error(monkeypatch):
    """Redis 异常 → fail-open 回 IDLE（观测/守卫辅助不该把用户挡在门外）。"""
    monkeypatch.setattr(lss, "_repo", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))
    assert read_phase("s-err") is LeadPhase.IDLE


# ── 确认闸：四种分支 ────────────────────────────────────────────────


async def test_gate_resumes_on_confirm(fake_repo):
    """ui_action=confirm + 槽在 → 短路恢复上轮目标（不走 LLM、不走降级）。"""
    lead_session_state.set_pending("s2", "清空画布上的全部内容")
    agent = LeadAgent(llm=None)

    kind, payload = await agent._confirm_guard(_ctx("s2", ui_action={"type": "confirm"}), "确认")

    assert kind == "resume", "应短路恢复，而不是继续走决策"
    assert payload.tool_args["goal"] == "清空画布上的全部内容", "恢复的必须是上轮原目标"
    assert payload.degraded is False, "恢复路径不是降级兜底"
    assert lead_session_state.pop_pending("s2") is None, "恢复后槽应已消费"


async def test_gate_reports_expired_when_no_slot(fake_repo):
    """槽已过期/不存在时点确认 → 明确提示，不误执行任何操作。"""
    agent = LeadAgent(llm=None)
    kind, text = await agent._confirm_guard(_ctx("s3", ui_action={"type": "confirm"}), "确认")
    assert kind == "text"
    assert "过期" in text


async def test_gate_cancels_and_clears_slot(fake_repo):
    """取消 → 确定性话术 + 槽被清掉（不留残渣影响后续轮次）。"""
    lead_session_state.set_pending("s4", "清空画布")
    agent = LeadAgent(llm=None)

    kind, text = await agent._confirm_guard(_ctx("s4", ui_action={"type": "cancel"}), "取消")
    assert kind == "text"
    assert "取消" in text
    assert lead_session_state.pop_pending("s4") is None, "取消也必须清槽"


async def test_gate_text_fallback_only_on_affirmative(fake_repo):
    """无显式信号时的文本兜底：只有肯定回复才恢复；寒暄/新指令不恢复。"""
    agent = LeadAgent(llm=None)

    lead_session_state.set_pending("s5", "删掉那个图")
    kind, payload = await agent._confirm_guard(_ctx("s5"), "确认")
    assert kind == "resume" and payload.tool_args["goal"] == "删掉那个图"

    lead_session_state.set_pending("s5", "删掉那个图")
    assert await agent._confirm_guard(_ctx("s5"), "你好") is None, "寒暄不该触发恢复"

    lead_session_state.set_pending("s5", "删掉那个图")
    assert await agent._confirm_guard(_ctx("s5"), "改成折线图") is None, "新指令不该触发恢复"


async def test_gate_passthrough_without_pending(fake_repo):
    """无槽且无显式信号 → None（走正常决策，守卫不干预）。"""
    agent = LeadAgent(llm=None)
    assert await agent._confirm_guard(_ctx("s6"), "画个图") is None


async def test_gate_illegal_ui_action_treated_as_no_signal(fake_repo):
    """非法 ui_action 一律当"无信号"处理（保守：宁可走正常决策，也不误判）。"""
    agent = LeadAgent(llm=None)
    assert await agent._confirm_guard(_ctx("s7", ui_action={"type": "bogus"}), "画个图") is None
    assert await agent._confirm_guard(_ctx("s7", ui_action="not-a-dict"), "画个图") is None


# ── SSE 事件契约：确认卡片靠它渲染 ───────────────────────────────────


def test_confirm_request_event_shape_is_rendered_by_frontend():
    """`confirm_request` 事件的字段（type/goal/question）必须与前端消费一致。

    前端 `canvasAssistantStore` 在 `case "confirm_request"` 里取 `goal` 与 `question`
    组装 `pendingConfirm`；`AIAssistant` 据它渲染 amber 卡片。字段改名 = 卡片静默失效。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    backend_src = (root / "app" / "services" / "agents" / "lead" / "lead_agent.py").read_text(encoding="utf-8")
    assert '"type": "confirm_request"' in backend_src, "后端不再发 confirm_request 事件"
    assert re.search(r'"type": "confirm_request",\s*\n\s*"goal"', backend_src), \
        "confirm_request 缺 goal 字段（前端 pendingConfirm 依赖它）"
    assert '"question"' in backend_src, "confirm_request 缺 question 字段"

    frontend = (root.parent / "frontend" / "src" / "stores" / "canvasAssistantStore.ts").read_text(encoding="utf-8")
    assert 'case "confirm_request"' in frontend, "前端不再处理 confirm_request 事件"
    assert "pendingConfirm" in frontend, "前端丢掉 pendingConfirm 状态（卡片无处渲染）"

    card = (root.parent / "frontend" / "src" / "pages" / "FreeCanvas" / "components" / "AIAssistant.tsx").read_text(encoding="utf-8")
    assert "pendingConfirm" in card and "确认执行" in card, "前端确认卡片已消失"


def test_entry_points_still_forward_ui_action():
    """两个入口都必须把 `ui_action` 透传进 LeadContext。

    这是最容易被重构改坏的一环（入口函数被重写过 → 漏传 → 卡片点了没反应）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "app" / "api" / "v1" / "ai.py").read_text(encoding="utf-8")
    n = src.count('ui_action=getattr(body, "ui_action", None)')
    assert n >= 2, f"两个入口都应透传 ui_action，实际只找到 {n} 处"
