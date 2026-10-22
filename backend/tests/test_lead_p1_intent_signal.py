# -*- coding: utf-8 -*-
"""理解层标签（intent）C+B：模型自述 + 代码推导兜底，请求级一次，不参与路由。

分层契约（lead_intent_signal 模块头）：
  intent   = 用户这一轮要什么产物（请求级 1 条，展示 + 监控理解漂移）
  decision = 这一步做什么、为什么（步级 N 条，执行动作）

本文件锁四件事：
  1. 只有**决策类**工具带 intent（观察类只是看一眼，不表达本轮目标）；
  2. 值域沿用 legacy 五值，认不出的值不猜（返回 None，交给 derived）；
  3. 循环里 intent 只发一次、且在 decision 之前；模型没给时标 source=derived；
  4. 同轮改主意 → 记 lead_intent_drift（不重发、不静默吞掉）。
"""
from __future__ import annotations

from app.services.agents.lead import lead_agent_unified as U
from app.services.agents.lead.lead_agent_tools import (
    DISPATCH_SPECS,
    OBSERVE_SPECS,
    TERMINAL_SPECS,
    lead_tool_specs,
)
from app.services.agents.lead.lead_intent_signal import (
    INTENT_VALUES,
    derive_intent,
    normalize_intent,
)


class FakeCtx:
    entry = "chat"
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
        self.calls = 0

    async def tool_call_round(self, messages, tools, **kw):
        self.calls += 1
        events = self.rounds.pop(0) if self.rounds else []
        for ev in events:
            yield ev


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": args}


async def _run(rounds, *, max_steps=6, ctx=None, **kw):
    llm = FakeLLM(rounds)
    ctx = ctx or FakeCtx()
    events = []
    async for ev in U.stream_unified(
        user_msg="你好", ctx=ctx, db_session=None, llm=llm,
        available_datasources=[], max_steps=max_steps, **kw,
    ):
        events.append(ev)
    return events, llm, ctx


# ── 工具面：只有决策类带 intent ──────────────────────────────────────────────

def test_decision_tools_declare_intent_enum():
    for spec in [*DISPATCH_SPECS, *TERMINAL_SPECS]:
        fn = spec["function"]
        props = fn["parameters"]["properties"]
        assert "intent" in props, f"{fn['name']} 缺 intent 字段"
        assert props["intent"]["enum"] == list(INTENT_VALUES)
        # 可选字段：模型漏填由 derived 兜底，绝不能因此让调用失败
        assert "intent" not in fn["parameters"].get("required", [])


def test_observe_tools_do_not_declare_intent():
    """观察类只是"看一眼"，不表达本轮目标——不塞字段，省 token 也避免误读。"""
    for spec in OBSERVE_SPECS:
        props = spec["function"]["parameters"]["properties"]
        assert "intent" not in props, f"{spec['function']['name']} 不该有 intent"


def test_tool_registry_still_hides_write_tools():
    """加字段不能顺手把可见性搞乱（回归护栏）。"""
    names = {s["function"]["name"] for s in lead_tool_specs()}
    assert not (names & {"add_chart_block", "remove_block", "clear_canvas"})


# ── 归一与推导（纯函数）────────────────────────────────────────────────────

def test_normalize_intent_accepts_five_values():
    for v in INTENT_VALUES:
        assert normalize_intent(v) == v
    assert normalize_intent("  ANALYSIS  ") == "analysis"
    assert normalize_intent("data-query") == "data_qa"
    assert normalize_intent("数据分析") == "analysis"


def test_normalize_intent_refuses_to_guess():
    """认不出的值不硬凑标签（宁可 derived 兜底，也不造假）。"""
    assert normalize_intent("") is None
    assert normalize_intent(None) is None
    assert normalize_intent("随便聊聊") is None


def test_derive_intent_matrix():
    # 画布入口派发/补发 → 在改画布
    assert derive_intent(entry="canvas", action="call_analysis", complexity="complex") == "canvas_edit"
    assert derive_intent(entry="canvas", action="call_analysis", complexity="simple") == "canvas_edit"
    # 对话入口：复杂 → 多步分析；简单 → 单次数值问答
    assert derive_intent(entry="chat", action="call_analysis", complexity="complex") == "analysis"
    assert derive_intent(entry="chat", action="call_analysis", complexity="simple") == "data_qa"
    # 没有派发动作（回答/反问/收尾）→ 对话
    assert derive_intent(entry="chat", action="answer") == "chat"
    assert derive_intent(entry="canvas", action="ask_user") == "chat"


# ── 循环：发一次、在 decision 之前、来源可辨 ───────────────────────────────

async def test_model_stated_intent_emitted_once_before_decision():
    events, _, _ = await _run([[_tc(
        "answer", {"text": "你好", "reason": "闲聊", "intent": "chat"},
    )]])
    intents = [e for e in events if e.get("type") == "intent"]
    assert len(intents) == 1
    assert intents[0]["intent"] == "chat"
    assert intents[0]["source"] == "model"
    assert intents[0]["degraded"] is False

    decision_idx = next(i for i, e in enumerate(events) if e.get("type") == "decision")
    intent_idx = events.index(intents[0])
    assert intent_idx < decision_idx, "intent 必须先于 decision（理解 → 执行）"


async def test_missing_intent_falls_back_to_derived():
    """模型没填 → 代码推导兜底，并明确标 derived（前端展示"（推断）"，不冒充）。"""
    events, _, _ = await _run([[_tc("answer", {"text": "你好", "reason": "闲聊"})]])
    intents = [e for e in events if e.get("type") == "intent"]
    assert len(intents) == 1
    assert intents[0]["source"] == "derived"
    assert intents[0]["intent"] == "chat"     # 无派发动作 → chat


async def test_intent_emitted_only_once_across_steps():
    events, _, _ = await _run([
        [_tc("get_canvas_layout", {}, "c1")],                                  # 观察：不发
        [_tc("answer", {"text": "a", "reason": "r", "intent": "data_qa"}, "c2")],
    ])
    intents = [e for e in events if e.get("type") == "intent"]
    assert len(intents) == 1
    assert intents[0]["intent"] == "data_qa"


async def test_intent_drift_is_recorded_not_resiliently_overwritten(monkeypatch):
    """同轮改主意：标签定稿不改，但必须留下漂移记录（理解漂移的唯一可观测信号）。

    只有 dispatch/repair 能"改主意"（终止动作会立刻收尾），所以这里把派发链路打桩。
    """
    async def _fake_worker(**kwargs):
        if False:      # pragma: no cover - 只为让它成为异步生成器
            yield {}

    monkeypatch.setattr(
        "app.services.agents.lead.lead_dispatch.run_worker", _fake_worker, raising=False)
    monkeypatch.setattr(
        "app.services.agents.lead.lead_worker.resolve_worker",
        lambda *a, **k: type("W", (), {"name": "fake"})(), raising=False)

    events, _, _ = await _run([
        [_tc("dispatch_analysis", {"goal": "分析销售", "complexity": "complex",
                                   "reason": "r1", "intent": "analysis"}, "c1")],
        [_tc("dispatch_analysis", {"goal": "再画个图", "complexity": "simple",
                                   "reason": "r2", "intent": "canvas_edit"}, "c2")],
    ])
    intents = [e for e in events if e.get("type") == "intent"]
    assert len(intents) == 1, "标签定稿后不再重发，避免同轮两条自相矛盾"
    assert intents[0]["intent"] == "analysis"
    done = [e for e in events if e.get("type") == "done"][-1]
    assert "lead_intent_drift" in done["degradations"], "漂移必须可观测，不能静默覆盖"
