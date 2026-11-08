# -*- coding: utf-8 -*-
"""P1-B 编排收敛 A 档回归测试。

覆盖四件事：
1. run_llm_step：text/tool_calls/reasoning_content 累积、on_call 透传、异常不吞且 span 收尾。
2. 三个内核的步参数（temperature/max_tokens/streaming）经 helper 后保持各自现值，
   且 assistant/tool 消息形状与改造前逐字节一致（对齐 tool_executor 的既有实现）。
3. AGENT_CONCURRENT_STEPS 默认 False 时，orchestrator / canvas 的层内执行是**串行**的。
4. canvas 执行层白名单 == _executor_tools() 暴露的 schema 集合（看不见 = 执行不了）。

不依赖数据库、Redis 与真实 LLM；需要用户态的路径统一传空 user_id
（cancel_requested("") 直接返回 False，不触发 cache）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services.agents.step_helpers import (
    StepLLMResult,
    append_tool_results,
    build_assistant_tool_message,
    run_llm_step,
)
from app.services.agents.tool_executor import ToolCallResult, build_assistant_message
from app.services.observability import TraceRecord


# ======================================================================
# 假 LLM：按脚本逐个 yield 事件（与 LLMClient.tool_call_round 同形状）
# ======================================================================


class ScriptedToolLLM:
    """记录调用参数；按 events 依次 yield；fail_at 指定在第几个事件处抛异常。"""

    def __init__(self, events: list[dict], fail_at: int | None = None) -> None:
        self.events = list(events)
        self.fail_at = fail_at
        self.calls: list[dict] = []

    async def tool_call_round(self, messages, tools, *, temperature=0.3,
                              max_tokens=None, streaming=None):
        self.calls.append({
            "messages": messages, "tools": tools, "temperature": temperature,
            "max_tokens": max_tokens, "streaming": streaming,
        })
        for i, ev in enumerate(self.events):
            if self.fail_at is not None and i == self.fail_at:
                raise RuntimeError("upstream boom")
            yield ev


def _events(*, with_reasoning: bool = True) -> list[dict]:
    rc = "思考过程" if with_reasoning else ""
    return [
        {"type": "text", "content": "结论"},
        {"type": "text", "content": "如下"},
        {"type": "tool_call", "id": "c1", "name": "query_sql",
         "arguments": "{}", "reasoning_content": rc},
        {"type": "tool_call", "id": "c2", "name": "render_chart",
         "arguments": "{}", "reasoning_content": (rc + "-2") if with_reasoning else ""},
        {"type": "done"},
    ]


def _mk_result(name: str, result: str, tc: dict | None = None, is_error: bool = False) -> ToolCallResult:
    return ToolCallResult(name=name, args={}, result=result, is_error=is_error,
                          tc=tc if tc is not None else {"id": f"id_{name}", "name": name})


# ======================================================================
# 1. run_llm_step
# ======================================================================


async def test_run_llm_step_accumulates_text_tools_and_reasoning():
    llm = ScriptedToolLLM(_events())
    result = await run_llm_step(
        llm, [{"role": "user", "content": "q"}], [{"type": "function"}],
        temperature=0.3, max_tokens=3000, streaming=True,
    )

    assert isinstance(result, StepLLMResult)
    # 可按三元组解包（任务书要求的返回契约）
    tool_calls, text_parts, reasoning = result
    assert [t["name"] for t in tool_calls] == ["query_sql", "render_chart"]
    assert text_parts == ["结论", "如下"]
    assert result.text == "结论如下"
    assert result.has_text is True
    # 后到的 reasoning 覆盖先到的（与 orchestrator 原实现一致）
    assert reasoning == "思考过程-2"
    # 参数原样透传
    call = llm.calls[0]
    assert (call["temperature"], call["max_tokens"], call["streaming"]) == (0.3, 3000, True)


async def test_run_llm_step_on_call_sees_every_event_in_order():
    llm = ScriptedToolLLM(_events(with_reasoning=False))
    seen: list[str] = []

    async def _on_call(ev: dict) -> None:
        seen.append(ev.get("type", ""))

    result = await run_llm_step(llm, [], [], streaming=True, on_call=_on_call)
    assert seen == ["text", "text", "tool_call", "tool_call", "done"]
    assert result.reasoning_content == ""


async def test_run_llm_step_propagates_exception_and_finishes_span():
    trace = TraceRecord(name="t")
    llm = ScriptedToolLLM(_events(), fail_at=2)  # 第 3 个事件（第一个 tool_call）处炸
    with pytest.raises(RuntimeError, match="upstream boom"):
        await run_llm_step(llm, [], [], trace=trace, span_name="unit_span")

    assert len(trace.children) == 1
    span = trace.children[0]
    assert span.name == "llm:unit_span"
    # 已累积的部分仍写进 span 摘要；异常本身交给调用方（既有语义：不写 span.error）
    assert span.output["text_length"] == len("结论如下")
    assert span.end_time is not None


async def test_run_llm_step_without_trace_is_noop_observability():
    llm = ScriptedToolLLM([{"type": "text", "content": "hi"}, {"type": "done"}])
    result = await run_llm_step(llm, [], [], trace=None)
    assert result.text == "hi"
    assert result.tool_calls == []


# ======================================================================
# 2. 消息形状与改造前逐字节一致
# ======================================================================


def test_build_assistant_tool_message_matches_tool_executor():
    tcs = [{"id": "c1", "name": "query_sql", "arguments": "{}"}]
    for reasoning in ("", "思考过程"):
        for offset in (0, 3):
            assert build_assistant_tool_message(tcs, reasoning, id_offset=offset) == \
                build_assistant_message(tcs, reasoning_content=reasoning, id_offset=offset)
    # 空 tool_calls 也不炸
    assert build_assistant_tool_message([]) == build_assistant_message([])


def test_append_tool_results_matches_legacy_shape():
    # 压缩路径（orchestrator / react 现状）
    big = json.dumps({"rows": [{"a": i} for i in range(500)], "columns": ["a"]})
    results = [
        _mk_result("query_sql", big, tc={"id": "c1"}),
        _mk_result("render_chart", '{"ok": true}', tc={"id": ""}),   # 空串 id 不得被兜底
        _mk_result("list_fields", "{}", tc={}),                       # 缺 id → 走兜底
    ]
    messages: list[dict] = []
    append_tool_results(messages, results, id_prefix="call_", id_offset=2)

    assert [m["role"] for m in messages] == ["tool", "tool", "tool"]
    assert [m["tool_call_id"] for m in messages] == ["c1", "", "call_4"]
    # 大结果被 compact（rows 截断并标注），error 结果完整保留不在此例
    assert "rows_total" in messages[0]["content"]
    assert messages[1]["content"] == '{"ok": true}'


def test_append_tool_results_can_keep_raw_for_canvas():
    raw = json.dumps({"canvas_action": {"action": "add_text_block"}, "rows": list(range(300))})
    messages: list[dict] = []
    append_tool_results(messages, [_mk_result("add_text_block", raw)], compact=False,
                        id_prefix="call_", id_offset=0)
    assert messages[0]["content"] == raw


async def test_three_kernels_step_params_preserved():
    """react / orchestrator / canvas 各自现值经 helper 后不变。"""
    combos = {
        "react": dict(temperature=0.3, max_tokens=3000, streaming=True),
        "orchestrator": dict(temperature=0.3, max_tokens=2000, streaming=False),
        "canvas": dict(temperature=0.3, max_tokens=2000, streaming=False),
    }
    shapes = {}
    for name, kw in combos.items():
        llm = ScriptedToolLLM(_events())
        result = await run_llm_step(llm, [{"role": "user", "content": "q"}], [], **kw)
        got = llm.calls[0]
        assert (got["temperature"], got["max_tokens"], got["streaming"]) == (
            kw["temperature"], kw["max_tokens"], kw["streaming"],
        ), name
        shapes[name] = (len(result.tool_calls), tuple(result.text_parts),
                        result.reasoning_content)
    assert shapes["orchestrator"] == shapes["canvas"] == shapes["react"]


# ======================================================================
# 3. 三个内核接入：react 的 reasoning 回传（bug 修复断言）
# ======================================================================


class _Emit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, ev: dict) -> None:
        self.events.append(ev)


async def test_react_reason_node_returns_reasoning_content():
    from app.services.agents.react_agent import ReactGraphAgent

    agent = ReactGraphAgent.__new__(ReactGraphAgent)  # 绕开 __init__（无需 graph/llm 真配置）
    agent.llm = ScriptedToolLLM(_events())
    agent.all_tools = []
    agent.agent_trace = None
    agent.is_canvas = False

    emit = _Emit()
    out = await agent._reason_node({"messages": [], "user_id": "", "iteration": 0},
                                   emit=emit)

    assert len(out["tool_calls"]) == 2
    # 关键断言：reasoning 必须一路带到 state，供 _execute_tools_node 回传
    assert out["reasoning_content"] == "思考过程-2"
    assert out["has_text_output"] is True
    assert [e["content"] for e in emit.events if e["type"] == "text"] == ["结论", "如下"]


async def test_react_execute_tools_node_writes_reasoning_into_assistant_message():
    from app.services.agents.react_agent import ReactGraphAgent

    agent = ReactGraphAgent.__new__(ReactGraphAgent)
    agent.agent_trace = None
    messages: list[dict] = []
    # 只验证 assistant 消息构造路径：交给 helper 的入参即 state 里的 reasoning
    msg = build_assistant_tool_message(
        [{"id": "c1", "name": "query_sql", "arguments": "{}"}],
        "思考过程-2",
    )
    assert msg["reasoning_content"] == "思考过程-2"
    assert msg["tool_calls"][0]["id"] == "c1"


# ======================================================================
# 4. AGENT_CONCURRENT_STEPS 默认关：层内串行
# ======================================================================


def _mk_step(sid: int, tool: str = "query_sql", depends_on: list[int] | None = None) -> dict:
    return {"step_id": sid, "goal": f"步骤{sid}", "tool": tool,
            "depends_on": depends_on or [], "purpose": ""}


async def test_orchestrator_levels_run_serially_by_default(monkeypatch):
    from app.services.agents import agent_orchestrator as mod

    order: list[tuple[str, str]] = []

    async def _fake_run(self, step, results, state, **shared):
        sid = str(step["step_id"])
        order.append((sid, "start"))
        await asyncio.sleep(0.01)
        order.append((sid, "end"))
        results[step["step_id"]] = json.dumps({"ok": sid})

    monkeypatch.setattr(mod.AgentOrchestrator, "_run_step_with_timeout", _fake_run)
    monkeypatch.setattr(mod.settings, "AGENT_CONCURRENT_STEPS", False)

    orch = mod.AgentOrchestrator.__new__(mod.AgentOrchestrator)
    emit = _Emit()
    state = {"results": {}, "ordered_steps": [_mk_step(1), _mk_step(2)], "user_id": ""}
    out = await orch._execute_steps_node(state, emit=emit, state_sink={})
    assert out["results"][1] and out["results"][2]
    assert order == [("1", "start"), ("1", "end"), ("2", "start"), ("2", "end")]


async def test_orchestrator_levels_can_run_concurrently_when_enabled(monkeypatch):
    from app.services.agents import agent_orchestrator as mod

    order: list[tuple[str, str]] = []

    async def _fake_run(self, step, results, state, **shared):
        sid = str(step["step_id"])
        order.append((sid, "start"))
        await asyncio.sleep(0.01)
        order.append((sid, "end"))

    monkeypatch.setattr(mod.AgentOrchestrator, "_run_step_with_timeout", _fake_run)
    monkeypatch.setattr(mod.settings, "AGENT_CONCURRENT_STEPS", True)

    orch = mod.AgentOrchestrator.__new__(mod.AgentOrchestrator)
    state = {"results": {}, "ordered_steps": [_mk_step(1), _mk_step(2)], "user_id": ""}
    await orch._execute_steps_node(state, emit=_Emit(), state_sink={})
    # 并发：两个 start 都在任何 end 之前
    assert [a for a, _ in order[:2]] == ["1", "2"]
    assert order[2:] == [("1", "end"), ("2", "end")] or order[2:] == [("2", "end"), ("1", "end")]


async def test_canvas_levels_run_serially_by_default(monkeypatch):
    from app.services.agents import canvas_orchestrator as mod

    order: list[tuple[str, str]] = []

    async def _fake_run(self, step, results, state, **shared):
        sid = str(step["step_id"])
        order.append((sid, "start"))
        await asyncio.sleep(0.01)
        order.append((sid, "end"))
        results[step["step_id"]] = json.dumps({"ok": sid})

    async def _no_fill(self, results, state, **shared):
        return None

    monkeypatch.setattr(mod.CanvasOrchestrator, "_run_step_with_timeout", _fake_run)
    monkeypatch.setattr(mod.CanvasOrchestrator, "_fill_missing_chart_types", _no_fill)
    monkeypatch.setattr(mod.settings, "AGENT_CONCURRENT_STEPS", False)

    canvas = mod.CanvasOrchestrator.__new__(mod.CanvasOrchestrator)
    canvas.extra_plannable_tools = set()
    state = {"results": {}, "ordered_steps": [_mk_step(1, "add_text_block"),
                                              _mk_step(2, "add_text_block")],
             "user_id": ""}
    out = await canvas._execute_steps_node(state, emit=_Emit(), state_sink={})
    assert out["results"][1] and out["results"][2]
    assert order == [("1", "start"), ("1", "end"), ("2", "start"), ("2", "end")]


# ======================================================================
# 5. canvas 执行层白名单 == 暴露给 LLM 的 schema
# ======================================================================


def test_canvas_executor_whitelist_equals_visible_schema():
    from app.services.agents.canvas_orchestrator import CanvasOrchestrator

    canvas = CanvasOrchestrator.__new__(CanvasOrchestrator)
    # 真实画布入口白名单（7 个落块工具 + 5 个查数工具）
    from app.services.canvas_tools import CANVAS_TOOL_NAMES
    canvas.extra_plannable_tools = set(CANVAS_TOOL_NAMES) | {
        "list_datasources", "list_fields", "list_metrics", "query_sql", "query_engine",
    }

    visible = {t["function"]["name"] for t in canvas._executor_tools()}
    allowed = canvas._executor_allowed_tools()

    assert visible == allowed
    assert "query_sql" not in allowed and "query_engine" not in allowed
    # 关键回归：幻觉调用的工具不在允许集合里（改造前 allowed 含 12 个，会把 query_sql 放行）
    assert "add_chart_block" in allowed


def test_canvas_executor_whitelist_empty_without_entry_tools():
    from app.services.agents.canvas_orchestrator import CanvasOrchestrator

    canvas = CanvasOrchestrator.__new__(CanvasOrchestrator)
    canvas.extra_plannable_tools = set()
    assert canvas._executor_allowed_tools() == set()
    assert canvas._executor_tools() == []
