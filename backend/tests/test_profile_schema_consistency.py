# -*- coding: utf-8 -*-
"""画像统计口径的唯一真源守卫（profile_schema）。

背景：2026-11-08 两条线并行做了"数据画像"——
- `agents/lead/lead_observe.profile_datasource`（给模型看，全表聚合）
- `services/datasource_profile.profile_datasource`（给前端/REST 看，可采样）
两处各写了一套类型判定与空值率，口径已经开始漂移（4 位 vs 6 位、16 vs 20 个类型标记）。
本文件锁三件事：
  1. 共享原语的行为（类型分类 / 空值率分母 / 标识符转义）；
  2. 两个消费方**不得再本地重定义**（棘轮：出现就红）；
  3. 聚合 SQL 真实跑出来的数字与共享口径一致（用真 DuckDB，不用假对象）。
"""
from __future__ import annotations

import pathlib
import re

import duckdb
import pytest

from app.services.profile_schema import (
    null_rate,
    quote_ident,
    supports_range,
    type_kind,
)

BACKEND = pathlib.Path(__file__).resolve().parents[1]
LEAD_OBSERVE = BACKEND / "app" / "services" / "agents" / "lead" / "lead_observe.py"
PROFILE_SERVICE = BACKEND / "app" / "services" / "datasource_profile.py"


# ── 1. 共享原语行为 ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "dtype,expected",
    [
        ("INTEGER", "numeric"),
        ("BIGINT", "numeric"),
        ("DECIMAL(18,2)", "numeric"),
        ("DOUBLE", "numeric"),
        ("TIMESTAMP", "temporal"),
        ("TIMESTAMP WITH TIME ZONE", "temporal"),
        ("DATE", "temporal"),
        ("TIME", "temporal"),
        ("VARCHAR", "other"),
        ("BLOB", "other"),
        ("JSON", "other"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_type_kind_classification(dtype, expected):
    assert type_kind(dtype) == expected


def test_supports_range_only_numeric_and_temporal():
    assert supports_range("INTEGER") and supports_range("DATE")
    # 字符串列的字典序极值对分析没有意义，不能给
    assert not supports_range("VARCHAR")


def test_null_rate_denominator_is_total_rows():
    """分母必须是**总行数**：问的是"这列有多少比例不可用"。"""
    assert null_rate(100, 75) == 0.25
    assert null_rate(4, 4) == 0.0
    assert null_rate(4, 0) == 1.0


def test_null_rate_edge_cases_do_not_lie():
    # 空表没有空值率——返回 None 而不是 0（0 会被误读成"这列很干净"）
    assert null_rate(0, 0) is None
    assert null_rate(None, None) is None
    # 脏输入（字符串数字）也要能吃
    assert null_rate("10", "8") == 0.2
    # 越界值被夹到 [0,1]（非空数 > 总数时不会算出负数）
    assert null_rate(10, 12) == 0.0


def test_quote_ident_escapes_double_quotes():
    assert quote_ident("销售额") == '"销售额"'
    assert quote_ident('a"b') == '"a""b"'


# ── 2. 棘轮：两个消费方不得再本地重定义口径 ────────────────────────────

_LOCAL_DEFS = (
    r"def\s+_type_kind\s*\(",
    r"def\s+_is_numeric\s*\(",
    r"def\s+_is_temporal\s*\(",
    r"def\s+_supports_range\s*\(",
    r"def\s+_norm_type\s*\(",
    r"_NUMERIC_TYPE_(MARKS|S)\s*=",
    r"_TEMPORAL_TYPE_(MARKS|S)\s*=",
)


@pytest.mark.parametrize("path", [LEAD_OBSERVE, PROFILE_SERVICE])
def test_consumers_do_not_redefine_shared_semantics(path):
    """出现本地重定义 → 红。

    这条不是洁癖：两套类型标记必然漂移（一条线加了 "NUMBER"，另一条没加），
    漂移的表现是"同一张表在两个入口的空值率/基数不同"，而没人会去核对。
    """
    src = path.read_text(encoding="utf-8-sig")
    hit = [pat for pat in _LOCAL_DEFS if re.search(pat, src)]
    assert not hit, f"{path.name} 又本地定义了画像口径：{hit}（请改用 app.services.profile_schema）"


@pytest.mark.parametrize("path", [LEAD_OBSERVE, PROFILE_SERVICE])
def test_consumers_import_shared_module(path):
    src = path.read_text(encoding="utf-8-sig")
    assert "profile_schema" in src, f"{path.name} 未引用共享画像口径模块"


# ── 3. 聚合 SQL 与共享口径一致（真 DuckDB） ───────────────────────────

def _agg(sql: str) -> dict:
    con = duckdb.connect()
    try:
        con.execute(
            'CREATE TABLE "data" (city VARCHAR, amount DOUBLE, ts DATE)'
        )
        con.execute(
            "INSERT INTO \"data\" VALUES "
            "('北京', 10.0, DATE '2026-01-01'),"
            "('上海', NULL, DATE '2026-02-01'),"
            "('北京', 30.0, NULL),"
            "(NULL, 20.0, DATE '2026-03-01')"
        )
        cols = [d[0] for d in con.execute(sql).description]
        row = con.execute(sql).fetchone()
        return dict(zip(cols, row))
    finally:
        con.close()


def test_aggregate_sql_matches_shared_semantics():
    """lead_observe.profile_sql 真跑一遍：行数/空值率/基数都要和共享口径对得上。"""
    from app.services.agents.lead.lead_observe import profile_sql

    fields = [
        {"name": "city", "data_type": "VARCHAR"},
        {"name": "amount", "data_type": "DOUBLE"},
        {"name": "ts", "data_type": "DATE"},
    ]
    got = _agg(profile_sql('"data"', fields))

    assert got["total_rows"] == 4
    # city: 4 行里 1 个 NULL → 0.25；distinct 只数非空 → 北京/上海 = 2
    assert got["c0__nd"] == 2
    assert null_rate(got["total_rows"], got["total_rows"] - got["c0__nulls"]) == 0.25
    # amount: 1 个 NULL
    assert null_rate(got["total_rows"], got["total_rows"] - got["c1__nulls"]) == 0.25
    # 数值列有 min/max/avg；字符串列不出现 min/max 别名
    assert got["c1__min"] == 10.0 and got["c1__max"] == 30.0
    assert got["c1__avg"] == pytest.approx(20.0)
    assert "c0__min" not in got, "字符串列不该有 min/max（字典序极值误导）"
    # 时间列有范围
    assert str(got["c2__min"]) == "2026-01-01"
