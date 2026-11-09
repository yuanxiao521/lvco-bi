"""消息级过程步骤投影（ai_messages.steps）：生成 / 落库 / 端点返回。

事实源是 agent_calls；本投影是 UI 物化视图——历史消息展开过程零查询成本，
且不受 agent_calls 明细 30 天清理影响。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.services.agent_trace import generate_steps_projection


def make_row(kind="tool", tool_name="query_sql", ok=True, dur=1200,
             preview='[{"rows": 1}]', error_kind=None, model=None,
             tokens_in=0, tokens_out=0):
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


async def test_projection_llm_rows_carry_tokens_and_model():
    db = _db_returning([make_row(kind="llm", tool_name="", model="glm-5.3-flash",
                                 tokens_in=1000, tokens_out=200, preview="")])
    steps = await generate_steps_projection(db, "t")
    assert steps[0]["kind"] == "llm"
    assert steps[0]["tokens"] == 1200
    assert steps[0]["model"] == "glm-5.3-flash"


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
