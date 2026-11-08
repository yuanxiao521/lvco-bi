# -*- coding: utf-8 -*-
"""P3-C：Lead 总预算闸 / 降级计数 / 首步流式开关（都在 lead_agent_unified）。

三个改动共享一条底线：**默认行为逐字节不变**。
  1. 预算闸是新闸（默认 20 次），只在超限时新增 status + 降级码；
  2. 降级计数是 done 事件的**附加**字段（degradations 列表原样保留）；
  3. 首步流式默认关；关的时候连 `streaming` 关键字都保持 False。

测试用假 LLM 驱动循环——真机评测只能证明"平均效果好"，
证明不了"绝不会超支""开关没开时行为没变"。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.agents.lead import lead_agent_unified as U
from app.services.agents.lead.lead_tools import RunAnalysisResult


# ── 假件（与 test_lead_p1_unified_loop 同构，额外记录 streaming 参数）──────

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
    """按脚本逐轮吐事件；记录调用次数与每次的 streaming 参数。"""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.calls = 0
        self.complete_calls = 0
        self.streaming_flags: list[object] = []
        self._settings = type("S", (), {"openai_model": "fake-model"})()

    async def tool_call_round(self, messages, tools, **kw):
        self.calls += 1
        self.streaming_flags.append(kw.get("streaming"))
        events = self.rounds.pop(0) if self.rounds else []
        for ev in events:
            yield ev

    async def complete(self, messages, **kw):
        self.calls += 1
        self.complete_calls += 1
        return "{}"


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": args}


async def _run(rounds, *, max_steps=6, **kw):
    llm = FakeLLM(rounds)
    ctx = FakeCtx()
    events: list[dict] = []
    async for ev in U.stream_unified(
        user_msg="你好", ctx=ctx, db_session=None, llm=llm,
        available_datasources=[], max_steps=max_steps, **kw,
    ):
        events.append(ev)
    return events, llm, ctx


def _done(events: list[dict]) -> dict:
    assert events and events[-1]["type"] == "done", f"最后一条不是 done: {events[-1:]}"
    return events[-1]


# ── ① 总预算闸 ────────────────────────────────────────────────────────────

async def test_budget_blocks_third_call(monkeypatch):
    """预算=2：前两次放行、**第三次调用被挡**，并走确定性收尾（不抛异常）。"""
    monkeypatch.setattr(settings, "LEAD_AGENT_MAX_LLM_CALLS", 2, raising=False)

    rounds = [[_tc("get_canvas_layout", {}, f"c{i}")] for i in range(6)]
    events, llm, _ = await run_with(rounds, max_steps=6)

    assert llm.calls == 2, f"预算=2 时最多 2 次 LLM 调用，实际 {llm.calls}"
    done = _done(events)
    assert U.LEAD_BUDGET_DEGRADATION in done["degradations"]
    assert done["degradation_counts"][U.LEAD_BUDGET_DEGRADATION] == 1
    # 收尾必须让用户看得见（status），且不再继续产出
    status_msgs = [e.get("message") for e in events if e.get("type") == "status"]
    assert U.LEAD_BUDGET_MESSAGE in status_msgs
    assert done["degraded"] is True


async def test_budget_zero_means_unlimited(monkeypatch):
    """0 = 不限：预算闸不介入，步数闸仍是唯一上限。"""
    monkeypatch.setattr(settings, "LEAD_AGENT_MAX_LLM_CALLS", 0, raising=False)

    rounds = [[_tc("get_canvas_layout", {}, f"c{i}")] for i in range(6)]
    events, llm, _ = await run_with(rounds, max_steps=3)

    assert llm.calls == 3, "0 应表示不限，步数闸（3）才是上限"
    done = _done(events)
    assert U.LEAD_BUDGET_DEGRADATION not in done["degradations"]
    assert "lead_agent_step_limit" in done["degradations"]


async def test_budget_counts_dispatch_internal_llm_calls(monkeypatch):
    """派发内部的 LLM 调用也要计入预算（这是"步数闸兜不住"的那部分）。

    假 run_worker 内部打 3 次 LLM：预算=2 时，第 1 步自己 1 次 + 派发 3 次 = 4，
    第 2 步在**调用前**就被挡 → 循环只发生 1 次 tool_call_round。
    """
    from app.services.agents.lead import lead_dispatch

    monkeypatch.setattr(settings, "LEAD_AGENT_MAX_LLM_CALLS", 2, raising=False)

    async def fake_run_worker(*, llm, on_result=None, **kw):
        # 模拟 worker 内部三次调用（报告 / 数值后验 / 重写），**都走 complete**
        await llm.complete([])
        await llm.complete([])
        await llm.complete([])
        if on_result is not None:
            on_result(RunAnalysisResult(success=True, report="ok", blocks_added=1))
        yield {"type": "text", "content": "子任务完成"}

    monkeypatch.setattr(lead_dispatch, "run_worker", fake_run_worker)

    rounds = [[_tc("dispatch_analysis", {"goal": "画图"}, f"c{i}")] for i in range(4)]
    events, llm, _ = await run_with(rounds, max_steps=4)

    # 循环自己的 tool_call_round 只发生 1 次：第 2 步在**调用前**就被预算闸挡住
    assert len(llm.streaming_flags) == 1, f"第 2 步不该再调 LLM，实际 {llm.streaming_flags}"
    # 派发内部的 3 次调用被代理计入总额（否则预算=2 根本不会触发）
    assert llm.complete_calls == 3
    done = _done(events)
    assert done["degradation_counts"].get(U.LEAD_BUDGET_DEGRADATION) == 1


# ── ② 降级计数（向下兼容）─────────────────────────────────────────────────

async def test_degradation_counts_distinguish_repeat_from_one_off(monkeypatch):
    """"配额被打满 3 次"与"偶发 1 次"必须能区分（这正是加计数的理由）。"""
    # 观察配额 = 2：5 步观察 → 2 次执行 + 3 次拒绝
    rounds = [[_tc("get_canvas_layout", {}, f"c{i}")] for i in range(5)]
    events, _, _ = await run_with(rounds, max_steps=5)

    done = _done(events)
    counts = done["degradation_counts"]
    assert counts.get("lead_agent_quota_observe") == 3, f"应记录被打满 3 次，实际 {counts}"
    # degradations 列表仍是原样的逐次记录（事件契约不变）
    assert done["degradations"].count("lead_agent_quota_observe") == 3
    assert sum(counts.values()) == len(done["degradations"])


async def test_degradation_counts_absent_when_nothing_degraded():
    """无降级时 counts 为空 dict（不是缺字段），前端无需判空分支。"""
    events, _, _ = await run_with([[_tc("answer", {"text": "你好"})]])
    done = _done(events)
    assert done["degraded"] is False
    assert done["degradations"] == []
    assert done["degradation_counts"] == {}


# ── ③ 首步流式开关：默认 False 时事件序列不变 ─────────────────────────────

async def test_stream_switch_off_keeps_event_sequence(monkeypatch):
    """默认关：事件序列与改造前一致，且 tool_call_round 收到 streaming=False。"""
    monkeypatch.setattr(settings, "LEAD_STREAM_FIRST_STEP", False, raising=False)

    events, llm, _ = await run_with([[_tc("answer", {"text": "你好"})]])

    assert [e["type"] for e in events] == ["intent", "decision", "report", "done"]
    assert llm.streaming_flags == [False], "开关关闭时必须显式传 streaming=False"
    assert not any(e["type"] == "text" for e in events), "关掉开关后不应出现中间 text 事件"


async def test_stream_switch_off_multistep_sequence_unchanged(monkeypatch):
    """多步 + 工具：关开关时序列里仍然没有中间 text 事件（与改造前逐字节同构）。"""
    monkeypatch.setattr(settings, "LEAD_STREAM_FIRST_STEP", False, raising=False)

    rounds = [[_tc("get_canvas_layout", {}, "c1")], [_tc("stop", {}, "c2")]]
    events, llm, _ = await run_with(rounds)

    assert [e["type"] for e in events] == [
        "tool_call", "tool_result", "intent", "decision", "done",
    ]
    assert llm.streaming_flags == [False, False]


# ── ④ 首步流式开关：True 时只有第 0 步流式 ────────────────────────────────

async def test_stream_first_step_forwards_text_immediately(monkeypatch):
    """开：第 0 步 streaming=True，text 事件在 tool_call 之前即时下发。"""
    monkeypatch.setattr(settings, "LEAD_STREAM_FIRST_STEP", True, raising=False)

    rounds = [
        [{"type": "text", "content": "我先看看"}, _tc("get_canvas_layout", {}, "c1")],
        [_tc("stop", {}, "c2")],
    ]
    events, llm, _ = await run_with(rounds)

    assert llm.streaming_flags == [True, False], "只有第 0 步流式，后续步骤必须非流式"
    types = [e["type"] for e in events]
    i_text = types.index("text")
    i_call = types.index("tool_call")
    assert i_text < i_call, "流式文字必须先于工具调用事件出现（否则等于没流式）"
    assert events[i_text]["content"] == "我先看看"


async def test_stream_first_step_does_not_duplicate_final_text(monkeypatch):
    """开：第 0 步流式后无工具调用 → 收尾只补 report，不重复吐同一段字。"""
    monkeypatch.setattr(settings, "LEAD_STREAM_FIRST_STEP", True, raising=False)

    events, _, _ = await run_with([[{"type": "text", "content": "直接回答"}]])

    texts = [e.get("content") for e in events if e.get("type") == "text"]
    reports = [e.get("content") for e in events if e.get("type") == "report"]
    assert texts == ["直接回答"], f"文本只应转发一次，实际 {texts}"
    assert reports == ["直接回答"], "report 仍要发（下游/记忆依赖它）"


# ── 计数代理自身 ──────────────────────────────────────────────────────────

async def test_counting_llm_proxy_counts_all_call_surfaces():
    """代理覆盖 5 个调用面，且非调用属性照常透传（worker 会读 _settings）。"""
    counter = {"count": 0}
    inner = FakeLLM([[_tc("stop", {})]])
    proxy = U._CountingLLM(inner, counter)

    async for _ in proxy.tool_call_round([], []):
        pass
    await proxy.complete([])
    assert counter["count"] == 2
    assert proxy._settings.openai_model == "fake-model", "属性透传失效会打挂 worker"
    assert proxy.streaming_flags == inner.streaming_flags


async def run_with(rounds, *, max_steps=6, **kw):
    return await _run(rounds, max_steps=max_steps, **kw)
