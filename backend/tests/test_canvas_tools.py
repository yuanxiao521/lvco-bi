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


async def _passthrough_resolve_measures(db, user_id, raw_measures, dimensions=None):  # noqa: ANN001
    """直通替身：把这些测试里的"普通度量"原样透传（不查指标库）。

    `resolve_measures` 是"指标语义层"引入的依赖（会把命中受治理指标的字段升级为
    指标引用）；本文件验证的是落块结构与缺参守卫，与指标解析无关，故替换为直通。
    """
    ms = list(raw_measures or [])
    return ms, ms  # (可执行度量, 前端展示度量) —— 普通度量两者同形


@pytest.fixture
def db_session() -> AsyncMock:
    """异步会话替身：工具内部会 await 会话上的方法，MagicMock 会在 await 处崩。"""
    return AsyncMock()


# ── add_chart_block ──────────────────────────────────────────────────────────

async def test_add_chart_block_success(db_session: MagicMock):
    """成功路径：返回带 canvas_action.add_chart_block 的结果，且携带 rows/columns。"""
    result = QueryResult(
        columns=["channel", "users"],
        rows=[{"channel": "抖音", "users": 100}, {"channel": "小红书", "users": 80}],
        query_time_ms=12,
    )
    with patch("app.services.canvas_tools.execute_chart_query", new=AsyncMock(return_value=result)), \
         patch("app.services.metric_service.resolve_measures", new=_passthrough_resolve_measures):
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


async def test_add_chart_block_kpi_card_no_dimension_allowed(db_session: MagicMock):
    """kpi_card 允许零维度：全表聚合单值，校验放行并走查询。"""
    result = QueryResult(
        columns=["sum_amount"],
        rows=[{"sum_amount": 1533082.61}],
        query_time_ms=8,
    )
    with patch("app.services.canvas_tools.execute_chart_query", new=AsyncMock(return_value=result)), \
         patch("app.services.metric_service.resolve_measures", new=_passthrough_resolve_measures):
        tool = AddChartBlockTool()
        out = await tool.execute(
            title="本月总销售额",
            chart_type="kpi_card",
            datasource_id=DS_ID,
            dimensions=[],
            measures=[{"field": "total_amount", "agg": "SUM"}],
            user_id=USER_ID,
            db_session=db_session,
        )
    parsed = json.loads(out)
    assert parsed["ok"] is True, out
    block = parsed["canvas_action"]["block"]
    assert block["queryConfig"]["dimensions"] == []
    assert block["rows"] == [{"sum_amount": 1533082.61}]


async def test_add_chart_block_requires_dimension_except_kpi(db_session: MagicMock):
    """非 kpi_card 空维度仍被拦截（触发 LLM 自纠错）。"""
    tool = AddChartBlockTool()
    out = await tool.execute(
        title="错误示例",
        chart_type="bar",
        datasource_id=DS_ID,
        dimensions=[],
        measures=[{"field": "total_amount", "agg": "SUM"}],
        user_id=USER_ID,
        db_session=db_session,
    )
    parsed = json.loads(out)
    assert "error" in parsed
    # 缺参守卫优先命中"缺少必填参数: dimensions"（早于后面那条"请改用 kpi_card"的提示）
    assert "dimensions" in parsed["error"], out


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
# ── 趋势图 X 轴排序：数据库 GROUP BY 不保证顺序（实测日期乱跳）──────────────

def test_sort_rows_by_date_for_trend_charts():
    from app.services.canvas_tools import _sort_rows_by_date

    # 维度名要像日期（_is_date_like 是按列名启发式判断的）
    rows = [
        {"order_date": "2025-03-27", "v": 1},
        {"order_date": "2024-07-10", "v": 2},
        {"order_date": "2025-01-31", "v": 3},
    ]
    out = _sort_rows_by_date(rows, ["order_date"], "line")
    assert [r["order_date"] for r in out] == ["2024-07-10", "2025-01-31", "2025-03-27"]


def test_sort_rows_keeps_order_when_not_date_or_not_trend():
    from app.services.canvas_tools import _sort_rows_by_date

    rank = [{"region": "East", "v": 9}, {"region": "South", "v": 1}]
    # 非日期维度：保持业务顺序（排名语义）
    assert _sort_rows_by_date(rank, ["region"], "bar") == rank
    # 日期维度但非趋势图：也保持原样
    date_rows = [{"order_date": "2025-03-01"}, {"order_date": "2024-01-01"}]
    assert _sort_rows_by_date(date_rows, ["order_date"], "pie") == date_rows


def test_sort_rows_handles_bucket_alias():
    from app.services.canvas_tools import _sort_rows_by_date

    rows = [{"order_date_month": "2025-03-01", "v": 1}, {"order_date_month": "2024-11-01", "v": 2}]
    out = _sort_rows_by_date(rows, ["order_date"], "line")
    assert [r["order_date_month"] for r in out] == ["2024-11-01", "2025-03-01"]