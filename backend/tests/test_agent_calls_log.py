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
from types import SimpleNamespace

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

# ══════════════════════════════════════════════════════════════════════════
# P4-B 追加：parent / error_layer / dispatch 行 / 续跑 turn_id
# （原有 10 条用例保持不变，只追加）
# ══════════════════════════════════════════════════════════════════════════


def test_parent_scope_sets_and_restores(recorder):
    """parent 是父子作用域：进入设置、退出还原到外层，不泄漏。"""
    _bind()
    agent_trace.set_parent("lead")
    with agent_trace.parent_scope("agent_orch"):
        agent_trace.record({"kind": "tool", "tool_name": "t", "ok": True})
    agent_trace.record({"kind": "llm", "ok": True})
    _run(agent_trace.flush())

    assert recorder.rows[0]["parent"] == "agent_orch"
    assert recorder.rows[1]["parent"] == "lead", "退出作用域后必须还原，不能泄漏给下一步"
    agent_trace.set_parent(None)


def test_parent_scope_restores_even_on_exception(recorder):
    _bind()
    agent_trace.set_parent("lead")
    with pytest.raises(RuntimeError):
        with agent_trace.parent_scope("canvas_orch"):
            raise RuntimeError("boom")
    agent_trace.record({"kind": "llm", "ok": True})
    _run(agent_trace.flush())
    assert recorder.rows[0]["parent"] == "lead"
    agent_trace.set_parent(None)


def test_error_layer_recorded_from_tool_error(monkeypatch, recorder):
    """工具错误里的 layer（L1-L4）要落到 agent_calls.error_layer 列。"""
    _bind()

    class _DenyTool:
        async def execute(self, **kwargs):
            return json.dumps({"error": "安全拦截（AST 校验）: 禁止 SELECT *", "layer": 4})

    monkeypatch.setattr(te.ToolRegistry, "get", staticmethod(lambda name: _DenyTool()))
    events: list[dict] = []
    ex = ToolExecutor(user_id="u1", db_session=None, emit=_fake_emit(events))
    _run(ex.execute_tool_call({"id": "c1", "name": "some_tool", "arguments": "{}"}))
    _run(agent_trace.flush())

    row = recorder.rows[0]
    assert row["ok"] is False
    assert row["error_layer"] == 4


def test_query_sql_guard_error_carries_layer():
    """query_sql 的 guard 拦截结果要带 layer：AST 折进 L3 时按 L4 记（不改 guard 实现）。

    注：P3-A 起 guard 移到数据源解析之后（裸表名自动补全需要 schema/known_tables），
    所以这里用假 db_session 提供最小 datasource，不需要真实数据库。
    """
    from app.services.agent_tools import QueryDatasourceTool

    class _Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                id="ds",
                name="demo",
                source_type="csv",
                schema_meta={"fields": []},
                connection_config={},
            )

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    out = _run(QueryDatasourceTool().execute(
        datasource_id="ds", sql="DROP TABLE t", user_id="u", db_session=_DB(),
    ))
    data = json.loads(out)
    assert "error" in data
    assert data.get("layer") == 4, data


# ── dispatch 行（含早返回路径）────────────────────────────────────────────


class _FakeCtx:
    entry = "chat"
    datasource_id = None
    canvas_state = None

    def __init__(self, session_id: str = "s1", user_id: str = "u1") -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.history_summary = ""
        self.metrics_ctx = ""
        self.extra_context = ""
        self.turn_summaries: list[str] = []
        self.turns: list[dict] = []

    def recent_turns(self, max_turns: int = 8, max_chars=None):
        return list(self.turns)

    def add_turn(self, role: str, content: str) -> None:
        self.turns.append({"role": role, "content": content})


class _FakeLLM:
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


def _install_dispatch_stubs(monkeypatch, *, result, call_on_result: bool = True):
    """把 run_worker 换成"立刻回调 on_result"的假实现，并桩掉评审阶段。"""
    import app.services.agents.lead.lead_dispatch as LD
    import app.services.agents.lead.lead_review as LR
    import app.services.agents.lead.lead_review_stage as LRS

    async def fake_run_worker(**kw):
        if call_on_result and result is not None:
            kw["on_result"](result)
        yield {"type": "tool_result", "name": "run_analysis", "result": "{}"}

    monkeypatch.setattr(LD, "run_worker", fake_run_worker)

    async def fake_review(*a, **k):
        return SimpleNamespace(text="[子任务评估] 上轮达标", passed=True, reason="", guidance="")

    monkeypatch.setattr(LRS, "run_review_stage", fake_review)
    monkeypatch.setattr(LR, "build_execution_summary", lambda *a, **k: "")


def _drive_unified(rounds, *, confirm_guard=None, ctx=None):
    import app.services.agents.lead.lead_agent_unified as U

    llm = _FakeLLM(rounds)
    ctx = ctx or _FakeCtx()
    events: list[dict] = []

    async def go():
        async for ev in U.stream_unified(
            user_msg="来", ctx=ctx, db_session=None, llm=llm,
            available_datasources=[], confirm_guard=confirm_guard,
        ):
            events.append(ev)

    _run(go())
    return events, ctx


def test_dispatch_row_recorded_on_early_return(monkeypatch, recorder):
    """早返回（直接 clear_canvas）也要有 dispatch 行 —— 行在 on_result 里落。"""
    _bind()
    early = SimpleNamespace(
        success=True, blocks_added=0, elapsed_ms=7,
        report="已清空画布，共移除 2 个块。", error=None,
    )
    _install_dispatch_stubs(monkeypatch, result=early)
    _drive_unified([
        [_tc("dispatch_analysis", {"goal": "清空画布", "complexity": "complex"})],
        [_tc("answer", {"text": "好"})],
    ])
    _run(agent_trace.flush())

    rows = [r for r in recorder.rows if r["kind"] == "dispatch"]
    assert len(rows) == 1, f"应恰好一条 dispatch 行，实际 {rows}"
    row = rows[0]
    assert row["ok"] is True
    assert row["parent"] == "lead"
    assert row["args_json"]["goal"] == "清空画布"
    assert row["args_json"]["blocks_added"] == 0, "早返回的 blocks_added=0 必须可见"
    assert row["duration_ms"] == 7
    assert "已清空画布" in (row["result_preview"] or "")
    assert row["mode"] == "orchestrator"


def test_dispatch_row_recorded_when_run_worker_returns_no_result(monkeypatch, recorder):
    """run_analysis 异常（不回调 on_result）时补一条失败 dispatch 行。"""
    _bind()
    _install_dispatch_stubs(monkeypatch, result=None, call_on_result=False)
    _drive_unified([
        [_tc("dispatch_analysis", {"goal": "分析一下"})],
        [_tc("answer", {"text": "好"})],
    ])
    _run(agent_trace.flush())

    rows = [r for r in recorder.rows if r["kind"] == "dispatch"]
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert rows[0]["error_msg"] == "run_analysis 未返回结果"


# ── turn_id 跨请求（HITL 续跑）────────────────────────────────────────────


def test_turn_id_persisted_and_taken_from_pending(monkeypatch):
    import app.services.lead_session_state as LSS
    from app.repositories.in_memory_cache import InMemoryCacheRepository

    repo = InMemoryCacheRepository()
    monkeypatch.setattr(LSS, "_repo", lambda: repo)
    old = str(uuid.uuid4())

    LSS.lead_session_state.set_pending("sess-1", "清空画布", turn_id=old)
    assert LSS.lead_session_state.pop_pending("sess-1") == "清空画布"
    assert LSS.lead_session_state.take_resumed_turn_id("sess-1") == old
    assert LSS.lead_session_state.take_resumed_turn_id("sess-1") is None, "一次性"


def test_resume_rebinds_original_turn_id(monkeypatch, recorder):
    """确认续跑（新请求）时，记录的 turn_id 复用挂起时的原值。"""
    import app.services.lead_session_state as LSS
    from app.repositories.in_memory_cache import InMemoryCacheRepository

    repo = InMemoryCacheRepository()
    monkeypatch.setattr(LSS, "_repo", lambda: repo)
    old_turn = str(uuid.uuid4())
    LSS.lead_session_state.set_pending("sess-1", "清空画布", turn_id=old_turn)
    # 模拟 lead_agent._confirm_guard 先 pop（新请求已绑了新的 turn_id）
    assert LSS.lead_session_state.pop_pending("sess-1") == "清空画布"
    _bind()   # 新请求的新 turn_id

    early = SimpleNamespace(
        success=True, blocks_added=0, elapsed_ms=3, report="已清空画布。", error=None,
    )
    _install_dispatch_stubs(monkeypatch, result=early)

    async def fake_guard(ctx, user_msg):
        return ("resume", SimpleNamespace(complexity="simple"))

    _drive_unified([], confirm_guard=fake_guard, ctx=_FakeCtx(session_id="sess-1"))
    _run(agent_trace.flush())

    rows = [r for r in recorder.rows if r["kind"] == "dispatch"]
    assert len(rows) == 1, rows
    assert rows[0]["turn_id"] == uuid.UUID(old_turn), "续跑的记录必须串回原 turn_id"

# ── parent 在真实编排路径上的落点（orchestrator 步执行 / 观察）──────────


def test_orchestrator_step_records_agent_orch_parent(monkeypatch, recorder):
    import app.services.agents.agent_orchestrator as AO

    _bind()
    orch = AO.AgentOrchestrator.__new__(AO.AgentOrchestrator)

    async def fake_step(self, step, results, state, **shared):
        agent_trace.record({"kind": "tool", "tool_name": "step_tool", "ok": True})
        results[step["step_id"]] = "{}"

    monkeypatch.setattr(AO.AgentOrchestrator, "_agentic_run_step", fake_step)
    _run(AO.AgentOrchestrator._run_step_with_timeout(
        orch, {"step_id": 1, "goal": "g"}, {}, {}, emit=_fake_emit([]),
    ))
    _run(agent_trace.flush())

    rows = [r for r in recorder.rows if r.get("tool_name") == "step_tool"]
    assert rows and rows[0]["parent"] == "agent_orch"
    assert agent_trace.current_parent() != "agent_orch", "退出步执行后必须还原"


def test_canvas_step_records_canvas_orch_parent(monkeypatch, recorder):
    import app.services.agents.canvas_orchestrator as CO

    _bind()
    orch = CO.CanvasOrchestrator.__new__(CO.CanvasOrchestrator)

    async def fake_step(self, step, results, state, **shared):
        agent_trace.record({"kind": "tool", "tool_name": "canvas_step_tool", "ok": True})
        results[step["step_id"]] = "{}"

    monkeypatch.setattr(CO.CanvasOrchestrator, "_agentic_run_step", fake_step)
    _run(CO.CanvasOrchestrator._run_step_with_timeout(
        orch, {"step_id": 1, "goal": "g"}, {}, {}, emit=_fake_emit([]),
    ))
    _run(agent_trace.flush())

    rows = [r for r in recorder.rows if r.get("tool_name") == "canvas_step_tool"]
    assert rows and rows[0]["parent"] == "canvas_orch"


def test_recon_tool_records_parent_and_tool_row(recorder):
    """观察调用绕过 ToolExecutor —— recon_tool 自己写 tool 行，parent=recon。"""
    from app.services.agents.lead import lead_observe

    _bind()
    agent_trace.set_parent("lead")

    class _Ctx:
        user_id = "u1"
        datasource_id = None
        metrics_ctx = "指标A: 销售额"

    value = _run(lead_observe.recon_tool("list_metrics", ctx=_Ctx(), args={}))
    assert value == "指标A: 销售额"
    _run(agent_trace.flush())

    rows = [r for r in recorder.rows if r.get("tool_name") == "recon:list_metrics"]
    assert rows, "观察调用必须留一条 tool 行（否则 parent=recon 永远不出现）"
    assert rows[0]["parent"] == "recon"
    assert rows[0]["ok"] is True
    assert agent_trace.current_parent() == "lead", "观察结束后 parent 必须还原"
    agent_trace.set_parent(None)
