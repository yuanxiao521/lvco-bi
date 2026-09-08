"""时间桶（dimension_buckets）专项测试：验证 query_engine 的 date_trunc 展开逻辑。

覆盖 P1-3 时间桶的核心纯函数：
- _build_select：SELECT 中展开 date_trunc 并生成桶别名（order_date_month）
- _build_group_by：GROUP BY 中按 date_trunc 表达式分组
- _normalize_sort：排序字段映射到桶别名
- QueryEngineTool：schema 暴露 dimension_buckets 参数
"""
import json

from app.services.agent_tools import QueryEngineTool
from app.services.query_engine import (
    ALLOWED_BUCKETS,
    _build_group_by,
    _build_select,
    _normalize_sort,
)

DIMS = ["order_date", "region"]
MEASURES = [{"field": "amount", "agg": "SUM"}]


def test_build_select_with_bucket_expands_date_trunc():
    """带桶维度展开为 date_trunc 表达式，且结果列名为桶别名。"""
    select_clause, result_columns = _build_select(
        DIMS, MEASURES, {"order_date": "month"}
    )
    assert "date_trunc('month', \"order_date\") AS \"order_date_month\"" in select_clause
    assert '"region"' in select_clause
    assert "SUM(\"amount\") AS \"sum_amount\"" in select_clause
    # 度量列名沿用源字段名（引擎既有行为）；桶维度列名用桶别名
    assert result_columns == ["order_date_month", "region", "amount"]


def test_build_select_without_bucket_keeps_raw():
    """无桶维度保持原样输出。"""
    select_clause, result_columns = _build_select(DIMS, MEASURES)
    assert '"order_date"' in select_clause
    assert '"region"' in select_clause
    assert result_columns == ["order_date", "region", "amount"]


def test_build_select_rejects_unsupported_bucket():
    """不支持的时间粒度（如 hour）降级为普通维度，不展开 date_trunc。"""
    select_clause, _ = _build_select(DIMS, MEASURES, {"order_date": "hour"})
    assert "date_trunc('hour'" not in select_clause
    assert '"order_date"' in select_clause


def test_allowed_buckets_cover_day_to_year():
    """允许的时间桶粒度为 day/week/month/quarter/year。"""
    assert ALLOWED_BUCKETS == {"day", "week", "month", "quarter", "year"}


def test_build_group_by_with_bucket():
    """GROUP BY 使用 date_trunc 表达式而非裸列名。"""
    assert _build_group_by(DIMS, {"order_date": "month"}) == (
        'GROUP BY date_trunc(\'month\', "order_date"), "region"'
    )


def test_build_group_by_without_bucket():
    """无桶时 GROUP BY 保持裸列名。"""
    assert _build_group_by(DIMS) == 'GROUP BY "order_date", "region"'
    assert _build_group_by([]) == ""


def test_normalize_sort_maps_dimension_to_bucket_alias():
    """对带桶维度排序时，排序字段映射到桶别名，避免 ORDER BY 裸列名报错。"""
    sort = {"field": "order_date", "order": "asc"}
    out = _normalize_sort(sort, DIMS, MEASURES, {"order_date": "month"})
    assert out["field"] == "order_date_month"


def test_normalize_sort_keeps_dimension_without_bucket():
    """无桶维度排序保持原字段。"""
    sort = {"field": "region", "order": "asc"}
    out = _normalize_sort(sort, DIMS, MEASURES)
    assert out["field"] == "region"


def test_query_engine_tool_schema_exposes_dimension_buckets():
    """QueryEngineTool schema 暴露 dimension_buckets 参数。"""
    schema = QueryEngineTool().schema()
    props = schema["function"]["parameters"]["properties"]
    assert "dimension_buckets" in props
    desc = json.dumps(schema, ensure_ascii=False)
    assert "month" in desc and "day" in desc