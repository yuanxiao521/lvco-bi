# -*- coding: utf-8 -*-
"""P1-C：agent_calls 采集器（B1-lite）单测。

覆盖任务要求的 5 件事：
① tool 行产生；② 白名单拒绝 → error_kind=whitelist；③ record 队列满不抛且 <1ms；
④ AGENT_CALL_LOG=off 零投递；⑤ 脱敏（SQL 字面量 → ?、敏感列保留线索）。
另加：llm 行 / turn 行 / 未绑定 trace 跳过 / flush 失败不抛 / seq 单调。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

import app.services.agent_trace as agent_trace
import app.services.agents.tool_executor as te
from app.services.agents.tool_executor import ToolExecutor


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    """替代 _write_batch 的内存收集器（不连 PG）。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def __call__(self, rows, db=None):
        self.rows.extend(rows)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    agent_trace.reset()
    # 关掉后台单写入者：测试只走显式 flush，结果才是确定的
    monkeypatch.setattr(agent_trace, "_autoflush", False)
    yield
    agent_trace.reset()


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(agent_trace, "_write_batch", rec)
    return rec


def _bind(**kw) -> tuple[str, str]:
    trace = str(uuid.uuid4())
    turn = str(uuid.uuid4())
    agent_trace.bind(trace_id=trace, turn_id=turn, entry=kw.pop("entry", "chat"), **kw)
    return trace, turn


def _fake_emit(events):
    async def emit(ev: dict) -> None:
        events.append(ev)

    return emit


def _fake_tool(result: str):
    class _Tool:
        async def execute(self, **kwargs):
            return result

    return _Tool()


def _patch_registry(monkeypatch, result: str, name: str = "query_sql") -> None:
    monkeypatch.setattr(
        te.ToolRegistry, "get", staticmethod(lambda n: _fake_tool(result) if n == name else None)
    )


# ── ① tool 行产生 + seq 单调 ──────────────────────────────────────────────


def test_tool_row_is_recorded(monkeypatch, recorder):
    _bind(user_id=uuid.uuid4(), session_id=uuid.uuid4(), message_id=uuid.uuid4())
    _patch_registry(monkeypatch, json.dumps({"columns": ["a"], "rows": [[1]], "row_count": 1}))
    events: list[dict] = []
    ex = ToolExecutor(user_id="u1", db_session=None, emit=_fake_emit(events))
    res = _run(ex.execute_tool_call(
        {"id": "call_1", "name": "query_sql", "arguments": '{"datasource_id": "ds-1", "sql": "SELECT 1"}'}
    ))
    assert res.is_error is False
    # 同 trace 第二条（llm 行）→ seq 必须递增
    agent_trace.record({"kind": "llm", "ok": True, "model": "m"})

    wrote = _run(agent_trace.flush())
    assert wrote == 2
    tool_row = recorder.rows[0]
    assert tool_row["kind"] == "tool"
    assert tool_row["tool_name"] == "query_sql"
    assert tool_row["tool_call_id"] == "call_1"
    assert tool_row["ok"] is True
    assert tool_row["error_kind"] is None
    assert tool_row["args_json"]["datasource_id"] == "ds-1"
    assert tool_row["duration_ms"] is not None and tool_row["duration_ms"] >= 0
    assert tool_row["entry"] == "chat"
    assert isinstance(tool_row["trace_id"], uuid.UUID)
    assert [r["seq"] for r in recorder.rows] == [0, 1]


# ── ② 白名单拒绝 → error_kind=whitelist ───────────────────────────────────


def test_whitelist_rejection_records_error_kind(recorder):
    _bind()
    events: list[dict] = []
    ex = ToolExecutor(
        user_id="u1", db_session=None, emit=_fake_emit(events), allowed_tools={"query_sql"}
    )
    res = _run(ex.execute_tool_call({"id": "call_2", "name": "render_chart", "arguments": "{}"}))
    assert res.is_error is True
    _run(agent_trace.flush())
    row = recorder.rows[0]
    assert row["ok"] is False
    assert row["error_kind"] == "whitelist"
    assert "不在当前入口允许范围" in (row["error_msg"] or "")


# ── ③ record 队列满：不抛、<1ms ───────────────────────────────────────────


def test_record_queue_full_does_not_raise_and_is_fast(monkeypatch):
    _bind()
    monkeypatch.setattr(agent_trace, "_QUEUE_MAX", 1)
    agent_trace.record({"kind": "tool", "ok": True})  # 占满
    started = time.perf_counter()
    for _ in range(2000):
        agent_trace.record({"kind": "tool", "ok": True})  # 全部丢弃
    elapsed = time.perf_counter() - started
    assert elapsed / 2000 < 0.001, f"record 太慢：{elapsed / 2000 * 1000:.3f}ms/次"
    assert agent_trace.stats()["dropped"] == 2000


# ── ④ off 时零投递 ────────────────────────────────────────────────────────


def test_off_means_zero_delivery(monkeypatch, recorder):
    _bind()
    monkeypatch.setattr(agent_trace, "log_mode", lambda: "off")
    agent_trace.record({"kind": "tool", "ok": False})
    assert agent_trace.stats()["enqueued"] == 0
    assert _run(agent_trace.flush()) == 0
    assert recorder.rows == []


# ── ⑤ 脱敏 ────────────────────────────────────────────────────────────────


def test_redaction_masks_sql_literals_and_unknown_args(recorder):
    _bind()
    agent_trace.record({
        "kind": "tool",
        "tool_name": "query_sql",
        "ok": True,
        "args": {
            "datasource_id": "ds-1",
            "sql": "SELECT * FROM t WHERE name = 'alice' AND age = 42",
            "password": "s3cret",
            "api_key": "k-123",
        },
    })
    _run(agent_trace.flush())
    args = recorder.rows[0]["args_json"]
    assert args["datasource_id"] == "ds-1"
    assert "alice" not in args["sql"]
    assert "42" not in args["sql"]
    assert "?" in args["sql"]
    assert args["password"] == "<masked>"
    assert args["api_key"] == "<masked>"


def test_sensitive_columns_keep_evidence(recorder):
    _bind()
    agent_trace.record({
        "kind": "tool",
        "tool_name": "query_sql",
        "ok": False,
        "error_kind": "sensitive_column",
        "args": {"datasource_id": "ds-1", "sql": "SELECT phone FROM t WHERE phone = '13800000000'"},
        "blocked_columns": ["phone"],
        "error_msg": "查询包含敏感列：phone",
    })
    _run(agent_trace.flush())
    args = recorder.rows[0]["args_json"]
    assert args["blocked_columns"] == ["phone"]
    assert "phone" in args["sql_masked"]
    assert "13800000000" not in args["sql_masked"]


def test_tool_executor_sensitive_error_keeps_blocked_columns(monkeypatch, recorder):
    """端到端：工具返回"敏感列"错误 → ToolExecutor 归类 + 抽出列名 → 保留线索。"""
    _bind()
    err = json.dumps(
        {"error": "查询包含敏感列，已被安全策略拦截：phone、id_card"}, ensure_ascii=False
    )
    _patch_registry(monkeypatch, err)
    events: list[dict] = []
    ex = ToolExecutor(user_id="u1", db_session=None, emit=_fake_emit(events))
    _run(ex.execute_tool_call(
        {"id": "c3", "name": "query_sql", "arguments": '{"datasource_id": "ds-1", "sql": "SELECT phone FROM t"}'}
    ))
    _run(agent_trace.flush())
    row = recorder.rows[0]
    assert row["error_kind"] == "sensitive_column"
    assert row["args_json"]["blocked_columns"] == ["phone", "id_card"]
    assert "phone" in row["args_json"]["sql_masked"]


# ── 附加：llm / turn / 未绑定 / flush 失败 ────────────────────────────────


def test_llm_and_turn_rows(recorder):
    _bind()
    agent_trace.record({
        "kind": "llm", "ok": True, "model": "glm-5.3-flash",
        "tokens_in": 100, "tokens_out": 20, "tokens_cached": 30,
    })
    agent_trace.record({
        "kind": "turn", "ok": True, "degraded": False,
        "rounds": 3, "tool_calls": 5, "tool_failures": 1, "duration_ms": 1234,
    })
    _run(agent_trace.flush())
    llm_row, turn_row = recorder.rows
    assert (llm_row["kind"], llm_row["tokens_in"], llm_row["tokens_cached"]) == ("llm", 100, 30)
    assert (
        turn_row["rounds"], turn_row["tool_calls"], turn_row["tool_failures"], turn_row["duration_ms"]
    ) == (3, 5, 1, 1234)


def test_unbound_trace_is_skipped():
    agent_trace.record({"kind": "tool", "ok": True})
    assert agent_trace.stats()["skipped_unbound"] == 1
    assert _run(agent_trace.flush()) == 0


def test_flush_error_never_raises(monkeypatch):
    _bind()

    async def _boom(rows, db=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_trace, "_write_batch", _boom)
    agent_trace.record({"kind": "tool", "ok": True})
    assert _run(agent_trace.flush()) == 0
    assert agent_trace.stats()["flush_errors"] == 1
