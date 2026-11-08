# -*- coding: utf-8 -*-
"""P0-3 取消：取消位 + 三个编排循环的优雅退出（后端 only）。

设计要点（为什么是"粘性取消位 + 新轮清除"而不是一次性布尔位）：
- 用户点停止 → 端点置位（非属主 404）；
- 同一轮内的**重派/重试**都应该继续停住（否则 Lead 一重派就又跑起来），
  所以取消位是**粘性**的（有值即取消），由"新的一轮真正开始"时清除；
- 三个循环在下一轮迭代前读到 → 按既有 **skipped 语义**收尾（不抛异常、不改事件类型）
  → 仍产报告 + done，前端才能解锁。

覆盖：
1. 取消位读写（含 clear / fail-open / 空 user 不动）；
2. 端点归属校验（非属主 404 / 属主置位）；
3. 取消后"下一步前退出"且**不再调 LLM**（三个循环各一条）；
4. 未设置取消位时行为不变（步骤照常执行 / 路由照常）。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.ai as ai_module
import app.services.agents.agent_orchestrator as AO
import app.services.agents.canvas_orchestrator as CO
import app.services.agents.react_agent as RA
from app.repositories.in_memory_cache import InMemoryCacheRepository


def _run(coro):
    return asyncio.run(coro)


def _collect(bucket: list[dict]):
    """异步 emit 收集器（生产代码是 `await emit(...)`）。"""
    async def emit(ev: dict) -> None:
        bucket.append(ev)
    return emit


# ── 1) 取消位：置位 / 读取 / 清除 / fail-open ──────────────────────────────


def test_cancel_flag_set_and_clear(monkeypatch):
    repo = InMemoryCacheRepository()
    monkeypatch.setattr(AO, "get_cache_repository", lambda: repo)

    assert AO.cancel_requested("user-1") is False, "未置位时不应取消"

    AO.request_cancel("user-1")
    assert AO.cancel_requested("user-1") is True

    AO.clear_cancel("user-1")
    assert AO.cancel_requested("user-1") is False, "新的一轮开始后必须能恢复"


def test_cancel_flag_is_sticky_until_cleared(monkeypatch):
    """同一次取消要能覆盖"这一轮里的重派"——不清就一直停（这是关键语义）。"""
    repo = InMemoryCacheRepository()
    monkeypatch.setattr(AO, "get_cache_repository", lambda: repo)

    AO.request_cancel("user-1")
    assert AO.cancel_requested("user-1") is True
    assert AO.cancel_requested("user-1") is True, "连续检查仍为 True（不是一次性消费）"
    AO.clear_cancel("user-1")
    assert AO.cancel_requested("user-1") is False


def test_cancel_key_reuses_ai_session_namespace_and_has_ttl(monkeypatch):
    """复用现有 ai:session:* key 空间；带 TTL（不会永久驻留）。"""
    repo = InMemoryCacheRepository()
    monkeypatch.setattr(AO, "get_cache_repository", lambda: repo)
    AO.request_cancel("user-1")
    keys = repo.keys()
    assert any(k.startswith("ai:session:cancel:") for k in keys), keys
    assert AO._CANCEL_TTL_SECONDS == 120


def test_cancel_fail_open_when_cache_down(monkeypatch):
    class _Boom:
        def get(self, key):
            raise RuntimeError("redis down")

        def set(self, *a, **k):
            raise RuntimeError("redis down")

        def delete(self, key):
            raise RuntimeError("redis down")

    monkeypatch.setattr(AO, "get_cache_repository", lambda: _Boom())
    assert AO.cancel_requested("user-1") is False, "缓存异常 → 读不到取消位，宁可不停"
    AO.request_cancel("user-1")  # 不能把用户挡在门外：必须不抛
    AO.clear_cancel("user-1")    # 清理失败也不能抛


def test_cancel_without_user_id_is_noop(monkeypatch):
    repo = InMemoryCacheRepository()
    monkeypatch.setattr(AO, "get_cache_repository", lambda: repo)
    AO.request_cancel(None)
    AO.request_cancel("")
    AO.clear_cancel(None)
    assert repo.keys() == []
    assert AO.cancel_requested(None) is False


# ── 2) 端点：非属主 404 / 属主置位 ────────────────────────────────────────


class _FakeResult:
    def __init__(self, obj) -> None:
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    def __init__(self, session) -> None:
        self._session = session

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._session)


def test_cancel_endpoint_404_for_non_owner(monkeypatch):
    called: list = []
    monkeypatch.setattr(AO, "request_cancel", lambda uid: called.append(uid))
    with pytest.raises(HTTPException) as ei:
        _run(ai_module.cancel_session_run(
            session_id="s-1",
            current_user=SimpleNamespace(id="user-a"),
            db=_FakeDB(None),  # 该 user 名下查不到这个会话 → 与"不存在"同口径 404
        ))
    assert ei.value.status_code == 404
    assert called == [], "非属主不得置取消位"


def test_cancel_endpoint_sets_flag_for_owner(monkeypatch):
    called: list = []
    monkeypatch.setattr(AO, "request_cancel", lambda uid: called.append(uid))
    session = SimpleNamespace(id="s-1", user_id="user-a")
    resp = _run(ai_module.cancel_session_run(
        session_id="s-1",
        current_user=SimpleNamespace(id="user-a"),
        db=_FakeDB(session),
    ))
    assert called == ["user-a"]
    assert resp.data["session_id"] == "s-1"
    assert "停止" in resp.data["message"]


# ── 3) agent_orchestrator：取消后跳过剩余步骤 → report 收尾 ────────────────


def _agent_orch():
    return AO.AgentOrchestrator.__new__(AO.AgentOrchestrator)


def test_agent_steps_skipped_before_llm_when_cancelled(monkeypatch):
    monkeypatch.setattr(AO, "cancel_requested", lambda *a, **k: True)
    events: list[dict] = []
    state = {
        "user_id": "u1",
        "ordered_steps": [
            {"step_id": 1, "tool": "query_engine"},
            {"step_id": 2, "tool": "query_sql"},
        ],
        "results": {},
    }
    shared = {"emit": _collect(events), "state_sink": {}}
    out = _run(AO.AgentOrchestrator._execute_steps_node(_agent_orch(), state, **shared))

    assert out["cancelled"] is True
    assert set(out["results"]) == {1, 2}
    for sid in (1, 2):
        payload = json.loads(out["results"][sid])
        assert payload["skipped"] is True and payload["cancelled"] is True
    assert shared["state_sink"]["results"] == out["results"]
    assert any(ev.get("phase") == "cancelled" for ev in events if ev.get("type") == "status")


def test_agent_steps_run_normally_without_cancel(monkeypatch):
    """未取消：行为不变（步骤照常跑，不写 cancelled）。"""
    monkeypatch.setattr(AO, "cancel_requested", lambda *a, **k: False)
    ran: list = []

    async def fake_step(self, step, results, state, **shared):
        ran.append(step["step_id"])
        results[step["step_id"]] = json.dumps({"ok": True})

    monkeypatch.setattr(AO.AgentOrchestrator, "_run_step_with_timeout", fake_step)
    events: list[dict] = []
    state = {
        "user_id": "u1",
        "ordered_steps": [{"step_id": 1, "tool": "query_engine"}],
        "results": {},
    }
    shared = {"emit": _collect(events), "state_sink": {}}
    out = _run(AO.AgentOrchestrator._execute_steps_node(_agent_orch(), state, **shared))

    assert "cancelled" not in out
    assert ran == [1]
    assert json.loads(out["results"][1]) == {"ok": True}


def test_agent_route_skips_chart_steps_when_cancelled():
    assert _run(AO.AgentOrchestrator._route_execute(_agent_orch(), {"cancelled": True, "has_chart_steps": True})) == "report"


# ── 4) canvas_orchestrator：取消后不补图/不重排，直接 finish ───────────────


def test_canvas_steps_skipped_and_no_extra_calls_when_cancelled(monkeypatch):
    monkeypatch.setattr(CO, "cancel_requested", lambda *a, **k: True)

    async def _boom(*a, **k):
        raise AssertionError("取消后不能再补图（那会多烧一次 LLM/落块）")

    monkeypatch.setattr(CO.CanvasOrchestrator, "_fill_missing_chart_types", _boom)
    orch = CO.CanvasOrchestrator.__new__(CO.CanvasOrchestrator)
    events: list[dict] = []
    state = {
        "user_id": "u1",
        "ordered_steps": [{"step_id": 1, "tool": "add_text_block"}],
        "results": {},
    }
    shared = {"emit": _collect(events), "state_sink": {}}
    out = _run(CO.CanvasOrchestrator._execute_steps_node(orch, state, **shared))

    assert out["cancelled"] is True
    assert json.loads(out["results"][1])["skipped"] is True
    assert any(ev.get("phase") == "cancelled" for ev in events if ev.get("type") == "status")


def test_canvas_steps_run_normally_without_cancel(monkeypatch):
    monkeypatch.setattr(CO, "cancel_requested", lambda *a, **k: False)
    fill_called: list = []

    async def fake_step(self, step, results, state, **shared):
        results[step["step_id"]] = json.dumps({"canvas_action": {"action": "add_text_block"}})

    async def fake_fill(self, results, state, **shared):
        fill_called.append(True)

    monkeypatch.setattr(CO.CanvasOrchestrator, "_run_step_with_timeout", fake_step)
    monkeypatch.setattr(CO.CanvasOrchestrator, "_fill_missing_chart_types", fake_fill)
    orch = CO.CanvasOrchestrator.__new__(CO.CanvasOrchestrator)
    events: list[dict] = []
    state = {
        "user_id": "u1",
        "ordered_steps": [{"step_id": 1, "tool": "add_text_block"}],
        "results": {},
    }
    shared = {"emit": _collect(events), "state_sink": {}}
    out = _run(CO.CanvasOrchestrator._execute_steps_node(orch, state, **shared))

    assert "cancelled" not in out
    assert fill_called == [True]


# ── 5) react_agent：取消后**不再调 LLM**，直接收尾并路由到 done ──────────


class _ForbiddenLLM:
    class _S:
        openai_model = "fake"

    _settings = _S()

    def tool_call_round(self, *a, **k):
        raise AssertionError("取消后不应再调用 LLM")


class _TextLLM:
    class _S:
        openai_model = "fake"

    _settings = _S()

    def __init__(self) -> None:
        self.calls = 0

    async def tool_call_round(self, messages, tools, **kw):
        self.calls += 1
        yield {"type": "text", "content": "## 结论\n" + "销售额 12345，" * 20}


def _react(llm) -> RA.ReactGraphAgent:
    agent = RA.ReactGraphAgent.__new__(RA.ReactGraphAgent)
    agent.llm = llm
    agent.all_tools = []
    agent.agent_trace = None
    agent.is_canvas = False
    return agent


def _react_state() -> dict:
    return {
        "messages": [{"role": "user", "content": "看一下销售额"}],
        "user_id": "u1",
        "iteration": 0,
        "query_call_count": 0,
    }


def test_react_exits_before_llm_call_when_cancelled(monkeypatch):
    monkeypatch.setattr(RA, "cancel_requested", lambda *a, **k: True)
    events: list[dict] = []
    agent = _react(_ForbiddenLLM())
    out = _run(RA.ReactGraphAgent._reason_node(
        agent, _react_state(), emit=_collect(events),
    ))
    assert out["done_reason"] == "cancelled"
    assert out["tool_calls"] == []
    assert any("停止" in ev.get("content", "") for ev in events if ev.get("type") == "text")
    # 路由必须走 done（不进 execute_tools）
    assert _run(RA.ReactGraphAgent._route_reason(agent, {**_react_state(), **out})) == "done"


def test_react_runs_normally_without_cancel(monkeypatch):
    monkeypatch.setattr(RA, "cancel_requested", lambda *a, **k: False)
    llm = _TextLLM()
    events: list[dict] = []
    out = _run(RA.ReactGraphAgent._reason_node(
        _react(llm), _react_state(), emit=_collect(events),
    ))
    assert llm.calls == 1, "未取消时应照常调用 LLM"
    assert "done_reason" not in out
    assert out["tool_calls"] == []
    assert any(ev.get("type") == "text" for ev in events)
