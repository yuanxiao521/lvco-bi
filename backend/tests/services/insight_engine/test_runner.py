"""InsightRunner 测试 - 全部用 mock，不依赖真实 DB / DuckDB / LLM"""

import uuid
from datetime import datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.datasource import DataSource, SourceType
from app.models.insight_rule import (
    InsightRule,
    ReportType,
    RunStatus,
    ScheduleType,
)
from app.models.notification import Notification, NotificationType
from app.services.insight_engine.detector import (
    Anomaly,
    AnomalyType,
    Severity,
    TimePoint,
)
from app.services.insight_engine.interpreter import InterpretResult
from app.services.insight_engine.runner import InsightRunner, InsightRunnerError


# ---------- 公共 fixture / helper ----------


def _make_rule(**overrides) -> InsightRule:
    """构造一个 InsightRule（不查 DB）"""
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
            "dimensions": [],
            "filters": [],
            "time_range_days": 30,
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
    """Mock AsyncSession: commit / refresh / add / execute 都不抛异常"""
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock(side_effect=_refresh_side_effect)
    db.add = MagicMock()
    db.execute = AsyncMock()
    return db


def _make_mock_interpreter():
    interp = MagicMock()
    interp.interpret = AsyncMock(
        return_value=InterpretResult(
            narrative="## 异常摘要\n测试叙述",
            summary="检测到 1 条异常",
            highlights=[
                {
                    "type": "anomaly",
                    "title": "amount 上升",
                    "description": "...",
                    "severity": "warning",
                }
            ],
            llm_model="test-model",
            llm_tokens_input=100,
            llm_tokens_output=50,
            raw_response="{}",
        )
    )
    return interp


def _make_mock_datasource(source_type: SourceType = SourceType.postgresql) -> MagicMock:
    ds = MagicMock()
    ds.source_type = source_type
    ds.connection_config = {}
    return ds


def _make_30day_rows() -> list[tuple]:
    """30 天数据，最后一天有尖峰"""
    return [
        (
            datetime(2026, 7, 1) + timedelta(days=i),
            100.0 + i * 10 + (250 if i == 29 else 0),
        )
        for i in range(30)
    ]


# ---------- _build_query_sql 测试 ----------


def test_build_query_sql_basic():
    """验证 SQL 含 table / time_field / measure agg / WHERE BETWEEN / GROUP BY"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    query_config = {
        "table": "orders",
        "time_field": "created_at",
        "measures": [{"field": "amount", "agg": "SUM"}],
    }
    period_start = datetime(2026, 7, 1)
    period_end = datetime(2026, 7, 31)

    sql, params = runner._build_query_sql(
        query_config, "test_schema", period_start, period_end
    )

    assert '"orders"' in sql
    assert '"created_at"' in sql
    assert 'SUM("amount")' in sql
    assert "BETWEEN ? AND ?" in sql
    assert 'GROUP BY "created_at"' in sql
    assert 'ORDER BY "created_at" ASC' in sql
    assert '"test_schema".public."orders"' in sql
    assert params == [period_start, period_end]


def test_build_query_sql_count_distinct():
    """agg=COUNT_DISTINCT 时生成 COUNT(DISTINCT "field")"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    query_config = {
        "table": "orders",
        "time_field": "created_at",
        "measures": [{"field": "user_id", "agg": "COUNT_DISTINCT"}],
    }

    sql, _ = runner._build_query_sql(
        query_config, "sch", datetime(2026, 7, 1), datetime(2026, 7, 31)
    )

    assert 'COUNT(DISTINCT "user_id")' in sql


def test_build_query_sql_invalid_agg_falls_back_to_sum():
    """agg='BAD' 时回退到 SUM"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    query_config = {
        "table": "orders",
        "time_field": "created_at",
        "measures": [{"field": "amount", "agg": "BAD"}],
    }

    sql, _ = runner._build_query_sql(
        query_config, "sch", datetime(2026, 7, 1), datetime(2026, 7, 31)
    )

    assert 'SUM("amount")' in sql
    assert 'BAD' not in sql


def test_build_query_sql_missing_table_raises():
    """query_config 无 table 时抛 InsightRunnerError"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    query_config = {
        "time_field": "created_at",
        "measures": [{"field": "amount", "agg": "SUM"}],
    }

    with pytest.raises(InsightRunnerError):
        runner._build_query_sql(
            query_config, "sch", datetime(2026, 7, 1), datetime(2026, 7, 31)
        )


def test_build_query_sql_rejects_injection_in_table():
    """table 含双引号/分号等特殊字符时抛 InsightRunnerError，防 SQL 注入"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    malicious_table = 'x"; DROP TABLE y; --'
    query_config = {
        "table": malicious_table,
        "time_field": "created_at",
        "measures": [{"field": "amount", "agg": "SUM"}],
    }

    with pytest.raises(InsightRunnerError, match="非法 SQL 标识符"):
        runner._build_query_sql(
            query_config, "sch", datetime(2026, 7, 1), datetime(2026, 7, 31)
        )


def test_build_query_sql_rejects_injection_in_field():
    """measure.field 含注入字符时抛 InsightRunnerError"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    query_config = {
        "table": "orders",
        "time_field": "created_at",
        "measures": [{"field": "amount) AS x; DROP TABLE y; --", "agg": "SUM"}],
    }

    with pytest.raises(InsightRunnerError, match="非法 SQL 标识符"):
        runner._build_query_sql(
            query_config, "sch", datetime(2026, 7, 1), datetime(2026, 7, 31)
        )


def test_build_query_sql_accepts_chinese_field_name():
    """中文字段名不在白名单内（Postgres 标识符规则），也应被拒绝"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    query_config = {
        "table": "orders",
        "time_field": "创建时间",
        "measures": [{"field": "amount", "agg": "SUM"}],
    }

    with pytest.raises(InsightRunnerError, match="非法 SQL 标识符"):
        runner._build_query_sql(
            query_config, "sch", datetime(2026, 7, 1), datetime(2026, 7, 31)
        )


# ---------- _rows_to_series 测试 ----------


def test_rows_to_series_basic():
    """正常 rows 转 TimePoint"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    measures = [{"field": "amount", "agg": "SUM"}]
    rows = [
        (datetime(2026, 7, 1), 100.0),
        (datetime(2026, 7, 2), 200.0),
    ]

    series = runner._rows_to_series(rows, "created_at", measures)

    assert len(series) == 2
    assert series[0].timestamp == datetime(2026, 7, 1)
    assert series[0].values == {"amount": 100.0}
    assert series[1].values == {"amount": 200.0}


def test_rows_to_series_string_timestamp():
    """ISO 字符串时间戳能解析"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    measures = [{"field": "amount", "agg": "SUM"}]
    rows = [
        ("2026-07-01T00:00:00", 100.0),
    ]

    series = runner._rows_to_series(rows, "created_at", measures)

    assert len(series) == 1
    assert series[0].timestamp == datetime(2026, 7, 1)


def test_rows_to_series_invalid_timestamp_skipped():
    """非法时间戳的行被跳过"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    measures = [{"field": "amount", "agg": "SUM"}]
    rows = [
        ("not-a-date", 100.0),
        (datetime(2026, 7, 2), 200.0),
    ]

    series = runner._rows_to_series(rows, "created_at", measures)

    assert len(series) == 1
    assert series[0].timestamp == datetime(2026, 7, 2)


def test_rows_to_series_null_value_becomes_zero():
    """None 值变 0.0"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    measures = [{"field": "amount", "agg": "SUM"}]
    rows = [
        (datetime(2026, 7, 1), None),
    ]

    series = runner._rows_to_series(rows, "created_at", measures)

    assert len(series) == 1
    assert series[0].values == {"amount": 0.0}


# ---------- _split_current_historical 测试 ----------


def test_split_current_historical():
    """10 个点的序列，current=7，historical=10"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    series = [
        TimePoint(timestamp=datetime(2026, 7, 1) + timedelta(days=i), values={"v": 1.0})
        for i in range(10)
    ]

    current, historical = runner._split_current_historical(series, current_days=7)

    assert len(current) == 7
    assert len(historical) == 10
    # current 是最后 7 个
    assert current[0].timestamp == datetime(2026, 7, 4)
    assert current[-1].timestamp == datetime(2026, 7, 10)


def test_split_current_historical_short_series():
    """5 个点的序列，current=5，historical=5"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    series = [
        TimePoint(timestamp=datetime(2026, 7, 1) + timedelta(days=i), values={"v": 1.0})
        for i in range(5)
    ]

    current, historical = runner._split_current_historical(series, current_days=7)

    assert len(current) == 5
    assert len(historical) == 5


# ---------- _serialize_anomalies 测试 ----------


def test_serialize_anomalies():
    """Anomaly 列表转 dict"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    anomalies = [
        Anomaly(
            type=AnomalyType.z_score,
            field="amount",
            severity=Severity.warning,
            current_value=250.0,
            expected_value=100.0,
            deviation=1.5,
            direction="up",
            description="z-score=3.2",
        )
    ]

    result = runner._serialize_anomalies(anomalies)

    assert len(result) == 1
    assert result[0]["type"] == "z_score"
    assert result[0]["field"] == "amount"
    assert result[0]["severity"] == "warning"
    assert result[0]["current_value"] == 250.0
    assert result[0]["expected_value"] == 100.0
    assert result[0]["deviation"] == 1.5
    assert result[0]["direction"] == "up"
    assert result[0]["description"] == "z-score=3.2"


# ---------- _compute_next_run 测试 ----------


def test_compute_next_run_daily():
    """daily 规则，下次运行是明天同一时间"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    rule = _make_rule(
        schedule=ScheduleType.daily,
        schedule_time=time(9, 0, 0),
    )
    now = datetime(2026, 7, 27, 10, 0, 0)

    next_run = runner._compute_next_run(rule, now)

    # candidate = 2026-07-27 09:00 <= now，所以 +1 天
    assert next_run == datetime(2026, 7, 28, 9, 0, 0)


def test_compute_next_run_weekly():
    """weekly 规则，下次运行是 7 天后"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    rule = _make_rule(
        schedule=ScheduleType.weekly,
        schedule_time=time(9, 0, 0),
    )
    now = datetime(2026, 7, 27, 10, 0, 0)

    next_run = runner._compute_next_run(rule, now)

    # weekly: candidate = 2026-07-27 09:00 + 7 天 = 2026-08-03 09:00
    assert next_run == datetime(2026, 8, 3, 9, 0, 0)


# ---------- _build_chart_payload 测试 ----------


def test_build_chart_payload():
    """验证 chart_type / title / config / data 结构"""
    runner = InsightRunner(interpreter=_make_mock_interpreter())
    series = [
        TimePoint(timestamp=datetime(2026, 7, 1), values={"amount": 100.0}),
        TimePoint(timestamp=datetime(2026, 7, 2), values={"amount": 200.0}),
    ]
    query_config = {
        "table": "orders",
        "time_field": "created_at",
        "measures": [{"field": "amount", "agg": "SUM"}],
    }

    payload = runner._build_chart_payload(series, query_config)

    assert payload["chart_type"] == "line"
    assert "created_at" in payload["title"]
    assert payload["config"]["x_field"] == "created_at"
    assert payload["config"]["y_fields"] == ["amount"]
    assert payload["config"]["aggs"] == ["SUM"]
    assert len(payload["data"]) == 2
    assert payload["data"][0]["timestamp"] == "2026-07-01T00:00:00"
    assert payload["data"][0]["amount"] == 100.0


# ---------- run() 端到端测试 ----------


@pytest.mark.anyio
async def test_run_success_end_to_end():
    """mock 全链路，验证成功流程"""
    db = _make_mock_db()
    interp = _make_mock_interpreter()
    runner = InsightRunner(interpreter=interp)
    rule = _make_rule()

    # mock db.execute 返回 datasource
    ds_mock = _make_mock_datasource(SourceType.postgresql)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ds_mock
    db.execute.return_value = result_mock

    period_start = datetime(2026, 7, 1)
    period_end = datetime(2026, 7, 31)

    with patch(
        "app.services.insight_engine.runner.duckdb_client"
    ) as mock_duckdb, patch(
        "app.services.insight_engine.runner.postgres_connector"
    ) as mock_pg, patch(
        "app.services.insight_engine.runner.get_encryption_key", return_value=b""
    ), patch(
        "app.services.insight_engine.runner.decrypt_value", return_value=""
    ):
        mock_duckdb.get_schema_name.return_value = "test_schema"
        mock_duckdb.execute.return_value = None
        mock_duckdb.fetchall.return_value = _make_30day_rows()
        mock_pg.get_attach_sql.return_value = "ATTACH '...'"

        record = await runner.run(db, rule, period_start, period_end)

    # 验证 record 状态
    assert record.status == RunStatus.success
    assert record.ai_narrative == "## 异常摘要\n测试叙述"
    assert record.charts is not None
    assert record.charts["chart_type"] == "line"
    assert record.raw_data is not None
    assert record.raw_data["total_points"] == 30
    assert record.detected_anomalies is not None
    assert "items" in record.detected_anomalies
    assert record.llm_model == "test-model"
    assert record.llm_tokens_input == 100
    assert record.llm_tokens_output == 50
    assert record.error_message is None

    # 验证 ReportGenerator 被调用: record.report_id 应被设置成关联 Report 的 id
    assert record.report_id is not None

    # 验证创建了 Report 对象 (source_type=ai_insight, status=published)
    added_objects = [call.args[0] for call in db.add.call_args_list]
    from app.models.report import Report, ReportSourceType, ReportStatus

    reports = [o for o in added_objects if isinstance(o, Report)]
    assert len(reports) == 1
    assert reports[0].source_type == ReportSourceType.ai_insight
    assert reports[0].status == ReportStatus.published
    assert reports[0].source_id == record.id
    assert reports[0].snapshot_blocks is not None
    assert "blocks" in reports[0].snapshot_blocks

    # 验证 interpreter.interpret 被调用
    interp.interpret.assert_called_once()

    # 验证 rule 被更新
    assert rule.last_run_at is not None
    assert rule.last_run_status == RunStatus.success
    assert rule.next_run_at is not None

    # 验证创建了 Notification (insight_ready)
    added_objects = [call.args[0] for call in db.add.call_args_list]
    notifications = [o for o in added_objects if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == NotificationType.insight_ready
    assert "测试日报" in notifications[0].title


@pytest.mark.anyio
async def test_run_notification_failure_does_not_rollback_success():
    """成功路径中 _push_notification 失败，不应回滚 record.status=success"""
    db = _make_mock_db()
    interp = _make_mock_interpreter()
    runner = InsightRunner(interpreter=interp)
    rule = _make_rule()

    ds_mock = _make_mock_datasource(SourceType.postgresql)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ds_mock
    db.execute.return_value = result_mock

    period_start = datetime(2026, 7, 1)
    period_end = datetime(2026, 7, 31)

    with patch(
        "app.services.insight_engine.runner.duckdb_client"
    ) as mock_duckdb, patch(
        "app.services.insight_engine.runner.postgres_connector"
    ) as mock_pg, patch(
        "app.services.insight_engine.runner.get_encryption_key", return_value=b""
    ), patch(
        "app.services.insight_engine.runner.decrypt_value", return_value=""
    ), patch.object(
        runner, "_push_notification", side_effect=RuntimeError("通知写入失败")
    ):
        mock_duckdb.get_schema_name.return_value = "test_schema"
        mock_duckdb.execute.return_value = None
        mock_duckdb.fetchall.return_value = _make_30day_rows()
        mock_pg.get_attach_sql.return_value = "ATTACH '...'"

        record = await runner.run(db, rule, period_start, period_end)

    # 关键断言：record 仍是 success，没被回滚成 failed
    assert record.status == RunStatus.success
    assert record.ai_narrative == "## 异常摘要\n测试叙述"
    # rule 也保持 success
    assert rule.last_run_status == RunStatus.success
    # 没有创建 insight_failed 通知（_push_notification 被 mock 后根本没写 Notification）
    added_objects = [call.args[0] for call in db.add.call_args_list]
    notifications = [o for o in added_objects if isinstance(o, Notification)]
    assert len(notifications) == 0


@pytest.mark.anyio
async def test_run_failure_pushes_insight_failed_notification():
    """mock duckdb_client.fetchall 抛异常，验证失败流程"""
    db = _make_mock_db()
    interp = _make_mock_interpreter()
    runner = InsightRunner(interpreter=interp)
    rule = _make_rule()

    ds_mock = _make_mock_datasource(SourceType.postgresql)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ds_mock
    db.execute.return_value = result_mock

    period_start = datetime(2026, 7, 1)
    period_end = datetime(2026, 7, 31)

    with patch(
        "app.services.insight_engine.runner.duckdb_client"
    ) as mock_duckdb, patch(
        "app.services.insight_engine.runner.postgres_connector"
    ) as mock_pg, patch(
        "app.services.insight_engine.runner.get_encryption_key", return_value=b""
    ), patch(
        "app.services.insight_engine.runner.decrypt_value", return_value=""
    ):
        mock_duckdb.get_schema_name.return_value = "test_schema"
        mock_duckdb.execute.return_value = None
        mock_duckdb.fetchall.side_effect = RuntimeError("DuckDB 连接断开")
        mock_pg.get_attach_sql.return_value = "ATTACH '...'"

        # run() 不应抛异常，返回 failed record
        record = await runner.run(db, rule, period_start, period_end)

    # 验证 record 状态
    assert record.status == RunStatus.failed
    assert record.error_message is not None
    assert "DuckDB 连接断开" in record.error_message
    assert "RuntimeError" in record.error_message

    # 验证 rule 状态
    assert rule.last_run_at is not None
    assert rule.last_run_status == RunStatus.failed
    assert rule.next_run_at is not None

    # 验证创建了 Notification (insight_failed)
    added_objects = [call.args[0] for call in db.add.call_args_list]
    notifications = [o for o in added_objects if isinstance(o, Notification)]
    assert len(notifications) == 1
    assert notifications[0].type == NotificationType.insight_failed

    # interpreter 不应被调用
    interp.interpret.assert_not_called()


@pytest.mark.anyio
async def test_run_empty_series_raises_and_fails():
    """mock fetchall 返回空列表，验证 record.status=failed"""
    db = _make_mock_db()
    interp = _make_mock_interpreter()
    runner = InsightRunner(interpreter=interp)
    rule = _make_rule()

    ds_mock = _make_mock_datasource(SourceType.postgresql)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ds_mock
    db.execute.return_value = result_mock

    period_start = datetime(2026, 7, 1)
    period_end = datetime(2026, 7, 31)

    with patch(
        "app.services.insight_engine.runner.duckdb_client"
    ) as mock_duckdb, patch(
        "app.services.insight_engine.runner.postgres_connector"
    ) as mock_pg, patch(
        "app.services.insight_engine.runner.get_encryption_key", return_value=b""
    ), patch(
        "app.services.insight_engine.runner.decrypt_value", return_value=""
    ):
        mock_duckdb.get_schema_name.return_value = "test_schema"
        mock_duckdb.execute.return_value = None
        mock_duckdb.fetchall.return_value = []  # 空结果
        mock_pg.get_attach_sql.return_value = "ATTACH '...'"

        record = await runner.run(db, rule, period_start, period_end)

    assert record.status == RunStatus.failed
    assert record.error_message is not None
    assert "查询结果为空" in record.error_message
    assert rule.last_run_status == RunStatus.failed
    interp.interpret.assert_not_called()


@pytest.mark.anyio
async def test_run_unsupported_datasource_fails():
    """mock datasource.source_type=mysql，验证 record.status=failed"""
    db = _make_mock_db()
    interp = _make_mock_interpreter()
    runner = InsightRunner(interpreter=interp)
    rule = _make_rule()

    ds_mock = _make_mock_datasource(SourceType.mysql)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = ds_mock
    db.execute.return_value = result_mock

    period_start = datetime(2026, 7, 1)
    period_end = datetime(2026, 7, 31)

    with patch(
        "app.services.insight_engine.runner.duckdb_client"
    ) as mock_duckdb, patch(
        "app.services.insight_engine.runner.postgres_connector"
    ) as mock_pg, patch(
        "app.services.insight_engine.runner.get_encryption_key", return_value=b""
    ), patch(
        "app.services.insight_engine.runner.decrypt_value", return_value=""
    ):
        mock_duckdb.get_schema_name.return_value = "test_schema"
        mock_duckdb.execute.return_value = None
        mock_duckdb.fetchall.return_value = _make_30day_rows()
        mock_pg.get_attach_sql.return_value = "ATTACH '...'"

        record = await runner.run(db, rule, period_start, period_end)

    assert record.status == RunStatus.failed
    assert record.error_message is not None
    assert "PostgreSQL" in record.error_message
    assert rule.last_run_status == RunStatus.failed
    # duckdb_client.fetchall 不应被调用
    mock_duckdb.fetchall.assert_not_called()
    interp.interpret.assert_not_called()
