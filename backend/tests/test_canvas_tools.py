"""画布操作工具单元测试。

隔离数据库与查询引擎，验证工具返回的 canvas_action 结构 / error 键。
成功：断言返回 JSON 含 canvas_action；失败：断言含 error 键（触发 Agent 自纠错）。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from app.services.canvas_tools import (
    AddChartBlockTool,
    AddTextBlockTool,
    ArrangeLayoutTool,
    RemoveBlockTool,
    UpdateChartBlockTool,
)
from app.schemas.query import QueryResult

USER_ID = "11111111-1111-1111-1111-111111111111"
DS_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def db_session() -> MagicMock:
    return MagicMock()


# ── add_chart_block ──────────────────────────────────────────────────────────

async def test_add_chart_block_success(db_session: MagicMock):
    """成功路径：返回带 canvas_action.add_chart_block 的结果，且携带 rows/columns。"""
    result = QueryResult(
        columns=["channel", "users"],
        rows=[{"channel": "抖音", "users": 100}, {"channel": "小红书", "users": 80}],
        query_time_ms=12,
    )
    with patch("app.services.canvas_tools.execute_chart_query", new=AsyncMock(return_value=result)):
        tool = AddChartBlockTool()
        out = await tool.execute(
            title="渠道获客对比",
            chart_type="grouped_bar",
            datasource_id=DS_ID,
            dimensions=["channel"],
            measures=[{"field": "users", "agg": "COUNT"}],
            user_id=USER_ID,
            db_session=db_session,
        )
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["canvas_action"]["action"] == "add_chart_block"
    block = parsed["canvas_action"]["block"]
    assert block["title"] == "渠道获客对比"
    assert block["chartType"] == "grouped_bar"
    assert block["queryConfig"]["dimensions"] == ["channel"]
    assert block["queryConfig"]["measures"][0] == {"field": "users", "agg": "COUNT"}
    assert len(block["rows"]) == 2
    assert block["columns"] == ["channel", "users"]


async def test_add_chart_block_query_error(db_session: MagicMock):
    """失败路径：查询抛错时返回 error 键（供 Agent 自纠错）。"""
    from app.services.query_engine import QueryEngineError

    with patch(
        "app.services.canvas_tools.execute_chart_query",
        new=AsyncMock(side_effect=QueryEngineError("字段无效", code="INVALID_FIELD")),
    ):
        tool = AddChartBlockTool()
        out = await tool.execute(
            title="t", chart_type="pie", datasource_id=DS_ID,
            dimensions=["channel"], measures=[{"field": "users", "agg": "COUNT"}],
            user_id=USER_ID, db_session=db_session,
        )
    parsed = json.loads(out)
    assert "error" in parsed


async def test_add_chart_block_missing_measures(db_session: MagicMock):
    """参数校验：无度量时直接返回 error。"""
    tool = AddChartBlockTool()
    out = await tool.execute(
        title="t", chart_type="pie", datasource_id=DS_ID,
        dimensions=["channel"], measures=[],
        user_id=USER_ID, db_session=db_session,
    )
    parsed = json.loads(out)
    assert "error" in parsed


# ── add_text_block ───────────────────────────────────────────────────────────

async def test_add_text_block_success(db_session: MagicMock):
    tool = AddTextBlockTool()
    out = await tool.execute(block_type="h2", content="一、用户分层结构", user_id=USER_ID, db_session=db_session)
    parsed = json.loads(out)
    assert parsed["canvas_action"]["action"] == "add_text_block"
    assert parsed["canvas_action"]["block"] == {"blockType": "h2", "content": "一、用户分层结构"}


async def test_add_text_block_invalid_type(db_session: MagicMock):
    tool = AddTextBlockTool()
    out = await tool.execute(block_type="h3", content="x", user_id=USER_ID, db_session=db_session)
    assert "error" in json.loads(out)


# ── update_chart_block ───────────────────────────────────────────────────────

async def test_update_chart_block_patch_fields(db_session: MagicMock):
    tool = UpdateChartBlockTool()
    out = await tool.execute(
        block_id="chart_123", title="新标题", chart_type="pie",
        dimensions=["channel"], measures=[{"field": "users", "agg": "SUM"}],
        user_id=USER_ID, db_session=db_session,
    )
    action = json.loads(out)["canvas_action"]
    assert action["action"] == "update_chart_block"
    assert action["blockId"] == "chart_123"
    assert action["patch"]["title"] == "新标题"
    assert action["patch"]["chartType"] == "pie"


async def test_update_chart_block_no_patch(db_session: MagicMock):
    tool = UpdateChartBlockTool()
    out = await tool.execute(block_id="chart_123", user_id=USER_ID, db_session=db_session)
    assert "error" in json.loads(out)


# ── remove_block / arrange_layout ────────────────────────────────────────────

async def test_remove_block(db_session: MagicMock):
    out = await RemoveBlockTool().execute(block_id="chart_9", user_id=USER_ID, db_session=db_session)
    action = json.loads(out)["canvas_action"]
    assert action["action"] == "remove_block"
    assert action["blockId"] == "chart_9"


async def test_arrange_layout(db_session: MagicMock):
    out = await ArrangeLayoutTool().execute(layout="auto", user_id=USER_ID, db_session=db_session)
    action = json.loads(out)["canvas_action"]
    assert action["action"] == "arrange_layout"
    assert action["layout"] == "auto"