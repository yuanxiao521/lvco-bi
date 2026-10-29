# -*- coding: utf-8 -*-
"""HITL 不退化（§6 不变量 7）：破坏性操作的确认卡片必须真的弹出来。

真机实测发现的漏洞：模型对"把画布清空"确实调了 ask_user，但**漏填 ask_kind**
（schema 里它是可选）→ ask_kind 退化成 clarify → 前端不渲染确认卡片，用户只看到
一句反问；而破坏性操作就悬在那里，可能被后续文本"确认"直接触发。

修法（两道）：
  1. schema：ask_kind 进 required（提示模型必须给）；
  2. 代码闸：effective_ask_kind —— 问题命中清空/删除等破坏性词而 ask_kind 不是
     confirm 时，强制升级（并记 lead_ask_kind_upgraded_to_confirm）。prompt 会失灵，代码闸不会。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.services.agents.lead import lead_agent_unified as U
from app.services.agents.lead.lead_agent_tools import TERMINAL_SPECS


class FakeCtx:
    entry = "canvas"
    session_id = "s1"
    user_id = "u1"
    datasource_id = None
    canvas_state = None

    def __init__(self) -> None:
        self.history_summary = ""
        self.metrics_ctx = ""
        self.turn_summaries: list[str] = []
        self.turns: list[dict] = []

    def recent_turns(self, max_turns: int = 8, max_chars=None):
        return list(self.turns)

    def add_turn(self, role: str, content: str) -> None:
        self.turns.append({"role": role, "content": content})


class FakeLLM:
    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)

    async def tool_call_round(self, messages, tools, **kw):
        events = self.rounds.pop(0) if self.rounds else []
        for ev in events:
            yield ev


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": args}


async def _run(rounds, *, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(
            "app.services.lead_session_state.set_pending", lambda *a, **k: None, raising=False)
    events = []
    async for ev in U.stream_unified(
        user_msg="把画布清空", ctx=FakeCtx(), db_session=None, llm=FakeLLM(rounds),
        available_datasources=[], max_steps=3,
    ):
        events.append(ev)
    return events


# ── 纯函数：effective_ask_kind ──────────────────────────────────────────────

def test_destructive_question_upgrades_to_confirm():
    kind, upgraded = U.effective_ask_kind("clarify", "确定要清空画布上的全部块吗？")
    assert (kind, upgraded) == ("confirm", True)


def test_non_destructive_question_stays_clarify():
    kind, upgraded = U.effective_ask_kind("clarify", "你想改哪张图的标题？")
    assert (kind, upgraded) == ("clarify", False)


def test_explicit_confirm_is_left_alone():
    kind, upgraded = U.effective_ask_kind("confirm", "要删除这张图吗？")
    assert (kind, upgraded) == ("confirm", False)


def test_illegal_ask_kind_normalizes_without_marking_upgrade():
    kind, upgraded = U.effective_ask_kind("whatever", "你想看哪个月份？")
    assert (kind, upgraded) == ("clarify", False)


# ── 循环：漏填 ask_kind 也必须弹出确认卡片 ─────────────────────────────────

async def test_loop_upgrades_missing_ask_kind_for_destructive_action(monkeypatch):
    events = await _run(
        [[_tc("ask_user", {"question": "确定要清空画布吗？", "reason": "不可恢复"})]],
        monkeypatch=monkeypatch,
    )
    confirms = [e for e in events if e.get("type") == "confirm_request"]
    assert confirms, "破坏性操作漏填 ask_kind 时必须由代码升级出确认卡片"
    done = [e for e in events if e.get("type") == "done"][-1]
    assert "lead_ask_kind_upgraded_to_confirm" in done["degradations"]


async def test_loop_keeps_clarify_for_normal_question(monkeypatch):
    events = await _run(
        [[_tc("ask_user", {"question": "要按哪个维度看？", "ask_kind": "clarify",
                           "reason": "信息不足"})]],
        monkeypatch=monkeypatch,
    )
    assert not [e for e in events if e.get("type") == "confirm_request"]


# ── schema / prompt：提示词里也要说清（模型先给对，代码闸兜底）─────────────

def test_ask_user_requires_ask_kind():
    spec = next(s for s in TERMINAL_SPECS if s["function"]["name"] == "ask_user")
    assert "ask_kind" in spec["function"]["parameters"]["required"]


def test_prompt_requires_confirm_for_destructive_ops():
    from app.services.ai_prompts import LEAD_AGENT_SYSTEM

    assert "破坏性操作" in LEAD_AGENT_SYSTEM
    assert "confirm" in LEAD_AGENT_SYSTEM


def test_fallback_prompt_matches_yaml():
    """yaml 与 ai_prompts 的 fallback 必须同源（否则改一处忘一处，线上行为不可预期）。"""
    from app.services import ai_prompts as ap

    yaml_path = Path(__file__).resolve().parents[1] / "prompts" / "lead_agent_system.yaml"
    yaml_text = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["system"]
    assert yaml_text.strip() == ap._LEAD_AGENT_SYSTEM_FALLBACK.strip()


# ── SSE 中继白名单（真机踩过的坑：事件产出正常但被中继吞掉）─────────────────

def test_sse_relays_forward_confirm_request_and_intent_source():
    """中继层是**白名单**：漏一个分支，事件就静默消失。

    实测踩过两次，都是"后端产出正常、前端永远收不到"：
      1) confirm_request 没进白名单 → 画布侧"清空画布"永不弹确认卡片；
      2) intent.source 没透传 → 前端无法区分"模型自述"与"代码推断"。
    这里用源码级断言钉死这两个契约（中继是内联生成器，不便单测调用）。
    """
    backend = Path(__file__).resolve().parents[1]
    ai_src = (backend / "app" / "api" / "v1" / "ai.py").read_text(encoding="utf-8")
    ct_src = (backend / "app" / "services" / "chat_task.py").read_text(encoding="utf-8")

    for name, src in (("ai.py", ai_src), ("chat_task.py", ct_src)):
        assert '"confirm_request"' in src, f"{name} 未中继 confirm_request（确认卡片会消失）"
        assert 'event.get("source")' in src, f"{name} 未透传 intent.source（推断标记会消失）"

def test_set_pending_uses_instance_method_not_module_import():
    """回归：unified 曾写 from lead_session_state import set_pending（模块级不存在）

    → ImportError 被吞 → pending 从未写入 → 确认卡片点确认永远走「已过期」分支。
    锁定：lead_session_state 模块必须提供实例 lead_session_state.set_pending。
    """
    from app.services import lead_session_state as m

    assert hasattr(m, 'lead_session_state'), '实例不存在'
    assert hasattr(m.lead_session_state, 'set_pending')
    assert not hasattr(m, 'set_pending'), '模块级 set_pending 不应存在（防止再次错误导入）'
