from app.services.insight_engine.auto_discovery import (
    ColumnInfo, TableInfo, discover_candidates,
    _is_time_name, _is_measure_name,
    _is_key_column,
    _build_table_infos_from_columns,
    build_suggestions_from_candidates,
    DiscoveryCandidate,
)
from uuid import uuid4


def test_is_time_name_chinese():
    assert _is_time_name("创建时间")
    assert _is_time_name("订单日期")


def test_is_time_name_english():
    assert _is_time_name("created_at")
    assert _is_time_name("order_date")


def test_is_measure_name_chinese():
    assert _is_measure_name("金额")
    assert _is_measure_name("销售总额")


def test_is_measure_name_english():
    assert _is_measure_name("amount")
    assert _is_measure_name("total_revenue")


def test_discover_candidates_basic():
    """含时间字段 + 度量字段的表应被识别"""
    tables = [
        TableInfo(
            name="orders",
            columns=[
                ColumnInfo("id", "int"),
                ColumnInfo("created_at", "timestamp"),
                ColumnInfo("amount", "decimal"),
                ColumnInfo("user_id", "varchar"),
            ],
            row_count_estimate=500,
        )
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.table == "orders"
    assert c.time_field == "created_at"
    assert "amount" in c.measure_fields
    assert "user_id" in c.dimension_fields
    assert c.confidence >= 0.9


def test_discover_candidates_no_time_field():
    """没有时间字段的表不应被识别"""
    tables = [
        TableInfo(
            name="users",
            columns=[
                ColumnInfo("id", "int"),
                ColumnInfo("name", "varchar"),
                ColumnInfo("age", "int"),
            ],
            row_count_estimate=100,
        )
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 0


def test_discover_candidates_too_few_rows():
    """行数过少的表不应被识别"""
    tables = [
        TableInfo(
            name="config",
            columns=[
                ColumnInfo("id", "int"),
                ColumnInfo("updated_at", "timestamp"),
                ColumnInfo("value", "int"),
            ],
            row_count_estimate=5,
        )
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 0


def test_discover_candidates_sort_by_confidence():
    """多个候选应按置信度降序"""
    tables = [
        TableInfo(
            name="small_table",
            columns=[
                ColumnInfo("date", "date"),
                ColumnInfo("amount", "int"),
            ],
            row_count_estimate=50,
        ),
        TableInfo(
            name="big_table",
            columns=[
                ColumnInfo("created_at", "timestamp"),
                ColumnInfo("revenue", "decimal"),
                ColumnInfo("cost", "decimal"),
            ],
            row_count_estimate=2000,
        ),
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 2
    assert candidates[0].table == "big_table"  # 分数更高
    assert candidates[1].table == "small_table"


def test_is_key_column_id():
    assert _is_key_column("id")
    assert _is_key_column("ID")


def test_is_key_column_foreign_key():
    assert _is_key_column("user_id")
    assert _is_key_column("order_id")


def test_is_key_column_not_key():
    assert not _is_key_column("amount")
    assert not _is_key_column("created_at")
    assert not _is_key_column("name")


def test_build_table_infos_filters_key_columns():
    """Key columns (id, *_id) should be filtered out so they aren't misclassified as measures."""
    tables_cols = {
        "orders": [
            ("id", "int"),
            ("user_id", "int"),
            ("created_at", "timestamp"),
            ("amount", "decimal"),
        ]
    }
    row_counts = {"orders": 500}
    table_infos = _build_table_infos_from_columns(tables_cols, row_counts)
    assert len(table_infos) == 1
    ti = table_infos[0]
    col_names = [c.name for c in ti.columns]
    assert "id" not in col_names  # filtered out
    assert "user_id" not in col_names  # filtered out
    assert "created_at" in col_names
    assert "amount" in col_names
    assert ti.row_count_estimate == 500


def test_build_suggestions_from_candidates():
    """Suggestion objects should be built correctly from candidates."""
    candidates = [
        DiscoveryCandidate(
            table="orders",
            time_field="created_at",
            measure_fields=["amount", "quantity"],
            dimension_fields=["user_id"],
            confidence=0.9,
            rationale="表 `orders` 含时间字段 `created_at` 和 2 个度量字段（约 500 行）",
        )
    ]
    row_counts = {"orders": 500}
    uid = uuid4()
    did = uuid4()
    suggestions = build_suggestions_from_candidates(candidates, row_counts, uid, did)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.table_name == "orders"
    assert s.time_field == "created_at"
    assert s.measure_fields == ["amount", "quantity"]
    assert s.confidence == 0.9
    assert s.row_count_estimate == 500
    assert s.suggested_name == "orders 日报"
    assert s.suggested_config["table"] == "orders"
    assert s.suggested_config["measures"][0]["field"] == "amount"
    # Verify status is pending (compare enum value)
    assert str(s.status.value) == "pending"
