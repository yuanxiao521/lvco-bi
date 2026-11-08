# -*- coding: utf-8 -*-
"""P3-A 工具契约：统一超时 + 结构化错误码 + 高频工具入参校验。

覆盖任务要求的 4 件事：
① 超时：慢工具 + 小 TOOL_TIMEOUT_SECONDS → TOOL_TIMEOUT（retryable/hint 齐全）；
② 非法参数：字段路径 + 中文原因（例：limit: 需要整数，收到 "top10"）；
③ 未知工具 / 白名单：code=UNKNOWN_TOOL / WHITELIST；
④ 向后兼容：结果仍是 {"error": ...}（is_error_result 为 True），老调用方不受影响。
另加：工具异常 → TOOL_EXCEPTION、合法参数照常执行、瞬时错误不写 memo、
      error_kind 映射不扩 agent_calls 的取值集合、add_chart_block/query_sample 靠名字表覆盖。
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import app.services.agents.tool_contract as tc
import app.services.agents.tool_executor as te
from app.services.agents.tool_contract import (
    EXISTING_ERROR_KINDS,
    ErrorCode,
    error_kind_for,
)
from app.services.agents.tool_executor import ToolExecutor


def _run(coro):
    return asyncio.run(coro)


class _Emit:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, ev: dict) -> None:
        self.events.append(ev)


class _SlowTool:
    async def execute(self, **kwargs):
        await asyncio.sleep(1.0)
        return json.dumps({"ok": True})


class _BoomTool:
    async def execute(self, **kwargs):
        raise RuntimeError("boom")


class _EchoTool:
    def __init__(self) -> None:
        self.called = False

    async def execute(self, **kwargs):
        self.called = True
        return json.dumps({"ok": True})


def _mk(monkeypatch, tools: dict, **kwargs):
    """构造执行器：注入假注册表 + 可控超时（避免依赖真实 60s 配置）。"""
    emit = _Emit()
    timeout = kwargs.pop("timeout", 60.0)
    monkeypatch.setattr(te, "settings", SimpleNamespace(TOOL_TIMEOUT_SECONDS=timeout))
    monkeypatch.setattr(te.ToolRegistry, "get", staticmethod(lambda name: tools.get(name)))
    ex = ToolExecutor(user_id="u1", db_session=None, emit=emit, **kwargs)
    return ex, emit


def _call(name: str, args: dict | None = None, *, cid: str = "call_1") -> dict:
    return {"id": cid, "name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}


# ── ① 统一超时 ────────────────────────────────────────────────────────────


def test_tool_timeout_returns_structured_error(monkeypatch):
    ex, _ = _mk(monkeypatch, {"slow_tool": _SlowTool()}, timeout=0.05)
    started = time.perf_counter()
    res = _run(ex.execute_tool_call(_call("slow_tool")))
    elapsed = time.perf_counter() - started

    data = json.loads(res.result)
    assert res.is_error is True
    assert data["error"] == "工具执行超时"
    assert data["code"] == "TOOL_TIMEOUT"
    assert data["retryable"] is True
    assert data["hint"], "超时必须给可执行的修正建议"
    assert elapsed < 0.8, f"超时未生效（{elapsed:.3f}s）"


def test_timeout_is_not_memoized(monkeypatch):
    """瞬时错误不能进 memo：否则一次超时会被缓存成'永远超时'。"""
    ex, _ = _mk(
        monkeypatch,
        {"list_fields": _SlowTool()},
        timeout=0.05,
        memo={},
        idempotent_tools=frozenset({"list_fields"}),
    )
    res = _run(ex.execute_tool_call(_call("list_fields", {"datasource_id": "ds-1"})))
    assert json.loads(res.result)["code"] == "TOOL_TIMEOUT"
    assert ex.memo == {}, "超时结果不应写入 memo"


# ── ② 非法参数：字段路径 + 原因 + 收到值 ──────────────────────────────────


def test_invalid_args_missing_required_has_field_path(monkeypatch):
    tool = _EchoTool()
    ex, _ = _mk(monkeypatch, {"query_sql": tool})
    res = _run(ex.execute_tool_call(_call("query_sql", {})))

    data = json.loads(res.result)
    assert res.is_error is True
    assert data["code"] == "INVALID_ARGS"
    assert data["retryable"] is True
    assert "sql" in data["error"]
    assert "缺少必填字段" in data["error"]
    assert tool.called is False, "参数非法时不应执行工具"


def test_invalid_args_type_error_mentions_field_and_value(monkeypatch):
    tool = _EchoTool()
    ex, _ = _mk(monkeypatch, {"query_engine": tool})
    res = _run(ex.execute_tool_call(
        _call("query_engine", {"datasource_id": "ds-1", "limit": "top10"})
    ))

    data = json.loads(res.result)
    assert data["code"] == "INVALID_ARGS"
    assert "limit" in data["error"], data["error"]
    assert "需要整数" in data["error"], data["error"]
    assert '"top10"' in data["error"], "要带上模型实际传的值，方便对照修正"
    assert tool.called is False


def test_invalid_args_emits_tool_result_event(monkeypatch):
    tool = _EchoTool()
    ex, emit = _mk(monkeypatch, {"query_engine": tool})
    _run(ex.execute_tool_call(_call("query_engine", {"datasource_id": "ds-1", "limit": "x"})))
    assert any(ev.get("type") == "tool_result" for ev in emit.events)


def test_add_chart_block_args_validated_by_name_table(monkeypatch):
    """add_chart_block 的实现在 canvas_tools.py（不在本次 scope），靠名字表覆盖。"""
    tool = _EchoTool()
    ex, _ = _mk(monkeypatch, {"add_chart_block": tool})
    res = _run(ex.execute_tool_call(
        _call("add_chart_block", {
            "title": "t", "chart_type": "bar", "datasource_id": "d",
            "dimensions": [], "measures": {"field": "x"},  # dict 而不是数组
        })
    ))
    data = json.loads(res.result)
    assert data["code"] == "INVALID_ARGS"
    assert "measures" in data["error"] and "需要数组" in data["error"]
    assert tool.called is False


def test_query_sample_optional_datasource(monkeypatch):
    tool = _EchoTool()
    ex, _ = _mk(monkeypatch, {"query_sample": tool})
    res = _run(ex.execute_tool_call(_call("query_sample")))
    assert res.is_error is False and tool.called is True


# ── ③ 未知工具 / 白名单 ───────────────────────────────────────────────────


def test_unknown_tool_code(monkeypatch):
    ex, _ = _mk(monkeypatch, {})
    res = _run(ex.execute_tool_call(_call("nope_tool")))
    data = json.loads(res.result)
    assert res.fatal is True, "未知工具仍是不可恢复错误（编排层据此终止步骤）"
    assert data["code"] == "UNKNOWN_TOOL"
    assert data["retryable"] is False


def test_whitelist_code(monkeypatch):
    ex, _ = _mk(monkeypatch, {"render_chart": _EchoTool()}, allowed_tools={"query_sql"})
    res = _run(ex.execute_tool_call(_call("render_chart")))
    data = json.loads(res.result)
    assert data["code"] == "WHITELIST"
    assert res.fatal is False
    assert data["retryable"] is False


# ── ④ 向后兼容：error 键仍在（is_error_result 依赖）──────────────────────


def test_error_key_still_present_for_every_error_code(monkeypatch):
    cases = [
        ("slow", {"slow_tool": _SlowTool()}, _call("slow_tool"), {"timeout": 0.05}),
        ("unknown", {}, _call("nope_tool"), {}),
        ("whitelist", {"render_chart": _EchoTool()}, _call("render_chart"), {"allowed_tools": {"query_sql"}}),
        ("invalid", {"query_engine": _EchoTool()}, _call("query_engine", {"datasource_id": "d", "limit": "x"}), {}),
        ("exception", {"render_chart": _BoomTool()}, _call("render_chart"), {}),
    ]
    for label, tools, tc, kw in cases:
        monkeypatch.undo()  # 每个 case 干净地重新 patch
        ex, _ = _mk(monkeypatch, tools, **kw)
        res = _run(ex.execute_tool_call(tc))
        data = json.loads(res.result)
        assert "error" in data and data["error"], f"{label}: 丢失向后兼容的 error 键"
        assert data.get("code"), f"{label}: 缺少 code"
        assert te.is_error_result(res.result) is True, f"{label}: is_error_result 必须仍为 True"


def test_tool_exception_code(monkeypatch):
    ex, _ = _mk(monkeypatch, {"render_chart": _BoomTool()})
    res = _run(ex.execute_tool_call(_call("render_chart")))
    data = json.loads(res.result)
    assert data["code"] == "TOOL_EXCEPTION"
    assert "boom" in data["error"]
    assert data["retryable"] is False


def test_valid_args_execute_normally(monkeypatch):
    tool = _EchoTool()
    ex, _ = _mk(monkeypatch, {"query_engine": tool})
    res = _run(ex.execute_tool_call(
        _call("query_engine", {"datasource_id": "ds-1", "limit": 10, "reason": "随便写写"})
    ))
    assert res.is_error is False
    assert tool.called is True, "合法参数必须真的执行工具"


# ── 与 agent_calls 的 error_kind 对齐（不扩表取值集合）───────────────────


def test_error_kind_mapping_keeps_agent_calls_contract():
    assert set(tc.CODE_TO_ERROR_KIND.values()) <= set(EXISTING_ERROR_KINDS), (
        "error_kind 只能落在既有取值集合内（agent_calls 表契约）"
    )
    assert error_kind_for(ErrorCode.WHITELIST) == "whitelist"
    assert error_kind_for(ErrorCode.UNKNOWN_TOOL) == "unknown_tool"
    assert error_kind_for(ErrorCode.SENSITIVE_COLUMN) == "sensitive_column"
    assert error_kind_for(ErrorCode.TIMEOUT) == "query_timeout"
    assert error_kind_for(ErrorCode.TOOL_EXCEPTION) == "tool_error"
    assert error_kind_for("NOT_A_REAL_CODE") is None


def test_timeout_records_existing_error_kind(monkeypatch):
    """超时落 agent_calls 时 error_kind 必须是既有值 query_timeout（不是 TOOL_TIMEOUT）。"""
    import app.services.agent_trace as agent_trace

    seen: list[dict] = []
    monkeypatch.setattr(agent_trace, "record", lambda ev: seen.append(ev))
    monkeypatch.setattr(agent_trace, "enabled", lambda: True)
    ex, _ = _mk(monkeypatch, {"slow_tool": _SlowTool()}, timeout=0.05)
    _run(ex.execute_tool_call(_call("slow_tool")))
    assert seen, "应产生一条 agent_calls 记录"
    assert seen[-1]["error_kind"] == "query_timeout"
