"""ReportGenerator 测试 - 全部用 mock，不依赖真实 DB"""

import uuid
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.insight_record import InsightRecord
from app.models.insight_rule import (
    InsightRule,
    ReportType,
    RunStatus,
    ScheduleType,
)
from app.models.report import ReportSourceType, ReportStatus
from app.services.insight_engine.report_generator import (
    ReportGenerator,
    ReportGeneratorError,
)


# ---------- 公共 helper ----------


def _make_record(**overrides) -> InsightRecord:
    defaults = dict(
        id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        datasource_id=uuid.uuid4(),
        run_at=datetime(2026, 7, 27, 9, 0, 0),
        period_start=datetime(2026, 6, 27),
        period_end=datetime(2026, 7, 27),
        status=RunStatus.success,
        error_message=None,
        ai_narrative="## 异常摘要\n测试叙述",
        charts={
            "chart_type": "line",
            "title": "趋势图",
            "config": {},
            "data": [{"x": 1}],
        },
        raw_data={
            "series": [
                {"timestamp": "2026-07-01", "values": {"amount": 100.0}}
            ],
            "total_points": 1,
        },
        detected_anomalies={
            "items": [
                {
                    "field": "amount",
                    "type": "z_score",
                    "severity": "warning",
                    "direction": "up",
                    "current_value": 250,
                    "expected_value": 100,
                    "deviation": 1.5,
                    "description": "...",
                }
            ],
            "count": 1,
        },
        llm_model="test",
        llm_tokens_input=10,
        llm_tokens_output=5,
        report_id=None,
    )
    defaults.update(overrides)
    return InsightRecord(**defaults)


def _make_rule(**overrides) -> InsightRule:
    defaults = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        datasource_id=uuid.uuid4(),
        name="测试日报",
        description=None,
        query_config={
            "table": "orders",
            "time_field": "created_at",
            "measures": [{"field": "amount", "agg": "SUM"}],
        },
        detect_types=["anomaly"],
        threshold=None,
        report_type=ReportType.daily_report,
        schedule=ScheduleType.daily,
        schedule_time=time(9, 0, 0),
        enabled=True,
        auto_created=False,
        last_run_at=None,
        last_run_status=None,
        next_run_at=None,
    )
    defaults.update(overrides)
    return InsightRule(**defaults)


async def _refresh_side_effect(obj, *args, **kwargs):
    """模拟 db.refresh：为缺 id 的对象补一个 uuid（mock flush 不触发 default）"""
    if getattr(obj, "id", None) is None:
        obj.id = uuid.uuid4()


def _make_mock_db():
    db = MagicMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock(side_effect=_refresh_side_effect)
    db.add = MagicMock()
    return db


# ---------- _build_title 测试 ----------


def test_build_title_format():
    """标题格式: '{rule_name} - {YYYY-MM-DD}'"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    rule = _make_rule(name="销售日报")
    record = _make_record(run_at=datetime(2026, 7, 27, 9, 0, 0))

    title = gen._build_title(rule, record)

    assert title == "销售日报 - 2026-07-27"


def test_build_title_truncates_long_rule_name():
    """rule.name 超过 200 字符时截断到 200"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    long_name = "x" * 250
    rule = _make_rule(name=long_name)
    record = _make_record(run_at=datetime(2026, 7, 27, 9, 0, 0))

    title = gen._build_title(rule, record)

    assert len(title) == 200
    assert title.startswith("x" * 200)


# ---------- _build_snapshot_blocks 测试 ----------


def test_build_snapshot_blocks_markdown():
    """ai_narrative 转成 markdown block"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(
        ai_narrative="## 摘要\n内容",
        charts=None,
        raw_data=None,
        detected_anomalies=None,
    )
    rule = _make_rule()

    blocks = gen._build_snapshot_blocks(record, rule)

    assert "blocks" in blocks
    markdown_blocks = [b for b in blocks["blocks"] if b["type"] == "markdown"]
    assert len(markdown_blocks) == 1
    assert markdown_blocks[0]["content"] == "## 摘要\n内容"


def test_build_snapshot_blocks_chart():
    """charts 字段转成 chart block"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(
        charts={
            "chart_type": "line",
            "title": "趋势图",
            "config": {"x_field": "ts"},
            "data": [{"x": 1}, {"x": 2}],
        }
    )
    rule = _make_rule()

    blocks = gen._build_snapshot_blocks(record, rule)

    chart_blocks = [b for b in blocks["blocks"] if b["type"] == "chart"]
    assert len(chart_blocks) == 1
    assert chart_blocks[0]["chartType"] == "line"
    assert chart_blocks[0]["title"] == "趋势图"
    assert chart_blocks[0]["config"] == {"x_field": "ts"}
    assert len(chart_blocks[0]["data"]) == 2


def test_build_snapshot_blocks_anomaly_table():
    """detected_anomalies 转成 table block，含 8 列"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(
        detected_anomalies={
            "items": [
                {
                    "field": "amount",
                    "type": "z_score",
                    "severity": "warning",
                    "direction": "up",
                    "current_value": 250,
                    "expected_value": 100,
                    "deviation": 1.5,
                    "description": "异常上升",
                }
            ],
            "count": 1,
        }
    )
    rule = _make_rule()

    blocks = gen._build_snapshot_blocks(record, rule)

    table_blocks = [b for b in blocks["blocks"] if b["type"] == "table"]
    anomaly_tables = [t for t in table_blocks if "异常" in t["title"]]
    assert len(anomaly_tables) == 1
    assert len(anomaly_tables[0]["columns"]) == 8
    assert anomaly_tables[0]["columns"] == [
        "字段",
        "类型",
        "严重性",
        "方向",
        "当前值",
        "预期值",
        "偏差",
        "描述",
    ]
    assert len(anomaly_tables[0]["rows"]) == 1
    row = anomaly_tables[0]["rows"][0]
    assert row[0] == "amount"
    assert row[1] == "z_score"
    assert row[2] == "warning"
    assert row[3] == "up"
    assert row[4] == 250
    assert row[5] == 100
    assert row[6] == "+150.0%"  # 1.5 = 150%
    assert row[7] == "异常上升"


def test_build_snapshot_blocks_raw_data_table():
    """raw_data.series 转成 table block，限 30 行"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    series = [
        {"timestamp": f"2026-07-{i:02d}", "values": {"amount": float(i)}}
        for i in range(1, 50)  # 49 行，首行 amount=1.0
    ]
    record = _make_record(
        raw_data={"series": series, "total_points": 49}
    )
    rule = _make_rule()

    blocks = gen._build_snapshot_blocks(record, rule)

    table_blocks = [b for b in blocks["blocks"] if b["type"] == "table"]
    raw_tables = [t for t in table_blocks if "原始数据" in t["title"]]
    assert len(raw_tables) == 1
    # 限 30 行
    assert len(raw_tables[0]["rows"]) == 30
    # 列：时间 + amount
    assert raw_tables[0]["columns"] == ["时间", "amount"]
    # 首行：timestamp=2026-07-01, amount=1.0 (range 起点 1)
    assert raw_tables[0]["rows"][0] == ["2026-07-01", 1.0]
    # 末行（第 30 行）：i=30, timestamp=2026-07-30, amount=30.0
    assert raw_tables[0]["rows"][-1] == ["2026-07-30", 30.0]
    # 标题包含总数
    assert "49" in raw_tables[0]["title"]


def test_build_snapshot_blocks_empty_charts():
    """charts=None 时不生成 chart block"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(
        charts=None,
        raw_data=None,
        detected_anomalies=None,
    )
    rule = _make_rule()

    blocks = gen._build_snapshot_blocks(record, rule)

    chart_blocks = [b for b in blocks["blocks"] if b["type"] == "chart"]
    assert len(chart_blocks) == 0
    # 仍应有 markdown block
    markdown_blocks = [b for b in blocks["blocks"] if b["type"] == "markdown"]
    assert len(markdown_blocks) == 1


def test_build_snapshot_blocks_empty_anomalies():
    """无异常时不生成异常 table"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(
        detected_anomalies={"items": [], "count": 0},
        raw_data=None,
    )
    rule = _make_rule()

    blocks = gen._build_snapshot_blocks(record, rule)

    table_blocks = [b for b in blocks["blocks"] if b["type"] == "table"]
    anomaly_tables = [t for t in table_blocks if "异常" in t["title"]]
    assert len(anomaly_tables) == 0


# ---------- generate_report 测试 ----------


@pytest.mark.anyio
async def test_generate_report_success():
    """验证成功路径: 创建 Report + 设置 record.report_id"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record()
    rule = _make_rule()

    report = await gen.generate_report(record, rule)

    # 验证 Report 字段
    assert report.user_id == record.user_id
    assert report.source_type == ReportSourceType.ai_insight
    assert report.source_id == record.id
    assert report.status == ReportStatus.published
    assert report.snapshot_blocks is not None
    assert "blocks" in report.snapshot_blocks
    # 标题格式
    assert "测试日报" in report.title
    assert "2026-07-27" in report.title

    # 验证 record.report_id 被设置为 report.id
    assert record.report_id == report.id
    assert record.report_id is not None

    # 验证 db.add 被调用（传入的是 Report 对象）
    db.add.assert_called_once()
    added = db.add.call_args.args[0]
    assert added is report
    # 验证 flush / refresh 被调用
    db.flush.assert_awaited_once()
    db.refresh.assert_awaited_once()


@pytest.mark.anyio
async def test_generate_report_raises_on_non_success_record():
    """record.status=failed 时抛 ReportGeneratorError"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(status=RunStatus.failed)
    rule = _make_rule()

    with pytest.raises(ReportGeneratorError, match="状态非 success"):
        await gen.generate_report(record, rule)

    # 不应写库
    db.add.assert_not_called()
    db.flush.assert_not_called()


@pytest.mark.anyio
async def test_generate_report_raises_on_empty_narrative():
    """ai_narrative=None 时抛 ReportGeneratorError"""
    db = _make_mock_db()
    gen = ReportGenerator(db)
    record = _make_record(ai_narrative=None)
    rule = _make_rule()

    with pytest.raises(ReportGeneratorError, match="无 ai_narrative"):
        await gen.generate_report(record, rule)

    # 不应写库
    db.add.assert_not_called()
    db.flush.assert_not_called()
