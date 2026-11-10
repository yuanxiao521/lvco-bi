"""消息级过程步骤投影（ai_messages.steps）：生成 / 落库 / 端点返回。

事实源是 agent_calls；本投影是 UI 物化视图——历史消息展开过程零查询成本，
且不受 agent_calls 明细 30 天清理影响。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.services.agent_trace import generate_steps_projection


def make_row(kind="tool", tool_name="query_sql", ok=True, dur=1200,
             preview='[{"rows": 1}]', error_kind=None, model=None,
             tokens_in=0, tokens_out=0, parent=None):
    r = MagicMock()
    r.kind = kind
    r.tool_name = tool_name
    r.ok = ok
    r.duration_ms = dur
    r.result_preview = preview
    r.error_kind = error_kind
    r.model = model
    r.tokens_in = tokens_in
    r.tokens_out = tokens_out
    r.parent = parent
    r.seq = 1
    return r


def _db_returning(rows):
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))))
    return db


async def test_projection_empty_without_trace_id():
    assert await generate_steps_projection(MagicMock(), None) == []
    assert await generate_steps_projection(MagicMock(), "") == []


async def test_projection_maps_tool_rows():
    db = _db_returning([make_row()])
    steps = await generate_steps_projection(db, "trace-1")
    assert len(steps) == 1
    s = steps[0]
    assert s["kind"] == "tool" and s["toolName"] == "query_sql"
    assert s["ok"] is True and s["durationMs"] == 1200
    assert s["id"] == "tool-1"
    assert "tokens" not in s


async def test_projection_excludes_llm_rows():
    """LLM 推理不进投影：过程面板只回答"调用了什么动作"。"""
    db = _db_returning([make_row(kind="llm", tool_name="", model="glm-5.3-flash",
                                 tokens_in=1000, tokens_out=200, preview="")])
    assert await generate_steps_projection(db, "t") == []


async def test_projection_carries_agent_attribution():
    """工具行带 parent 归属；dispatch 一律算 Lead；无 parent 视为 Lead 直连。"""
    db = _db_returning([
        make_row(kind="dispatch", tool_name=None, parent="lead"),
        make_row(tool_name="query_sql", parent="agent_orch"),
        make_row(tool_name="list_fields", parent=None),
    ])
    steps = await generate_steps_projection(db, "t")
    assert [s["agent"] for s in steps] == ["lead", "agent_orch", "lead"]


async def test_projection_only_actions():
    """投影只含 tool/dispatch（llm/turn 都被过滤）。"""
    db = _db_returning([
        make_row(kind="llm", tool_name="", preview=""),
        make_row(kind="tool", tool_name="run_query"),
        make_row(kind="turn"),
    ])
    steps = await generate_steps_projection(db, "t")
    assert [s["kind"] for s in steps] == ["tool"]


async def test_projection_marks_errors():
    db = _db_returning([make_row(ok=False, error_kind="timeout")])
    steps = await generate_steps_projection(db, "t")
    assert steps[0]["ok"] is False
    assert steps[0]["errorKind"] == "timeout"


async def test_projection_summary_truncated():
    db = _db_returning([make_row(preview="x" * 500)])
    steps = await generate_steps_projection(db, "t")
    assert len(steps[0]["summary"]) <= 120


async def test_projection_db_failure_returns_empty():
    db = MagicMock()
    db.execute = MagicMock(side_effect=RuntimeError("db down"))
    assert await generate_steps_projection(db, "t") == []


async def test_projection_filters_out_turn_rows():
    """turn 汇总行不进投影（投影只含 tool/llm/dispatch 过程事实）。"""
    db = _db_returning([make_row(kind="turn")])
    steps = await generate_steps_projection(db, "t")
    assert steps == []
