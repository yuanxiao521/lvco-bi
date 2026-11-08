# -*- coding: utf-8 -*-
"""数据画像服务（P1-E）单元测试。

全部用假的 DuckDB 客户端 + 假会话，不连真实 PostgreSQL / DuckDB 文件。
覆盖：
  1. 小表全量统计（行数/空值率/基数/极值/样例值）；
  2. 超阈值降级为采样，且 sampled=true、sample_size 如实上报、row_count 仍是真实值；
  3. USING SAMPLE 不被支持时回退 LIMIT 子查询（仍然标 sampled=true）；
  4. 敏感列只给统计量、不出 sample_values，且不发起取原始值的查询；
  5. 字段过滤（顺序/未知字段报错）；
  6. 属主校验：非属主 → 服务层 DatasourceNotFoundError；端点 → 404；
  7. 单列统计失败不影响其它列。
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.models.datasource import SourceType
from app.services import datasource_profile as profile_mod
from app.services.datasource_profile import (
    MAX_PROFILE_FIELDS,
    SAMPLE_ROWS,
    SAMPLE_ROW_THRESHOLD,
    DatasourceNotFoundError,
    DatasourceProfileError,
    InvalidFieldError,
    profile_datasource,
)

USER_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER_USER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DS_ID = UUID("12345678-1234-1234-1234-1234567890ab")


# ── 假会话 / 假 DuckDB ───────────────────────────────────────────────

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """只实现服务用到的 execute() → scalar_one_or_none()。"""

    def __init__(self, ds):
        self.ds = ds
        self.executed = 0

    async def execute(self, stmt):  # noqa: ARG002
        self.executed += 1
        return _FakeResult(self.ds)


class FakeDuck:
    """按 SQL 形态返回预设结果的假 DuckDB 客户端。

    stats: {列名: (total, non_null, distinct, min, max)}
    samples: {列名: [值, ...]}
    """

    def __init__(self, row_count=10, stats=None, samples=None,
                 supports_sampling=True, sample_size=SAMPLE_ROWS, fail_fields=()):
        self.row_count = row_count
        self.stats = stats or {}
        self.samples = samples or {}
        self.supports_sampling = supports_sampling
        self.sample_size = sample_size
        self.fail_fields = set(fail_fields)
        self.queries: list[str] = []

    def get_schema_name(self, user_id, datasource_id, datasource_name="", db_name=""):  # noqa: ARG002
        did = str(datasource_id).replace("-", "")[:8]
        safe = re.sub(r"[^0-9A-Za-z_]", "_", str(datasource_name or ""))[:16]
        return f"{safe}_{did}"

    def fetchall(self, sql, params=None):  # noqa: ARG002
        self.queries.append(sql)

        # 统计/样例查询也会带 "USING SAMPLE"（src 里含采样子句），所以必须先
        # 按语义分支（DISTINCT / CAST）匹配，再判断采样探测语句。
        if "COUNT(DISTINCT" in sql:
            name = re.search(r'COUNT\("([^"]+)"\)', sql).group(1)
            if name in self.fail_fields:
                raise RuntimeError(f"binder error: unsupported column {name}")
            total, non_null, distinct, mn, mx = self.stats.get(
                name, (self.row_count, self.row_count, 0, None, None)
            )
            return [(total, non_null, distinct, mn, mx)]

        if "CAST(" in sql:
            name = re.search(r'SELECT DISTINCT CAST\("([^"]+)"', sql).group(1)
            return [(v,) for v in self.samples.get(name, [])]

        if "USING SAMPLE" in sql:
            if not self.supports_sampling:
                raise RuntimeError("USING SAMPLE is not supported for this table")
            return [(self.sample_size,)]

        if sql.strip().startswith("SELECT COUNT(*) FROM"):
            return [(self.row_count,)]

        raise AssertionError("unexpected SQL: " + sql)


def make_ds(fields, *, table_name="data", user_id=USER_ID):
    meta = {"table_name": table_name, "fields": fields}
    return SimpleNamespace(
        id=DS_ID,
        user_id=user_id,
        name="sales",
        source_type=SourceType.csv,
        schema_meta=meta,
    )


def field(name, data_type="VARCHAR", **extra):
    item = {"name": name, "data_type": data_type, "category": "dimension"}
    item.update(extra)
    return item


def use_fake_duck(monkeypatch, fake):
    monkeypatch.setattr(profile_mod, "duckdb_client", fake)
    return fake


def stat_queries(fake):
    return [q for q in fake.queries if "COUNT(DISTINCT" in q]


# ── 1. 小表全量统计 ──────────────────────────────────────────────────

async def test_small_table_returns_full_stats(monkeypatch):
    fake = use_fake_duck(monkeypatch, FakeDuck(
        row_count=10,
        stats={
            "amount": (10, 8, 5, 12.5, 99.0),
            "region": (10, 10, 3, "华东", "西南"),  # 字符串列不给极值
        },
        samples={"amount": ["12.5", "99"], "region": ["华东", "华南"]},
    ))
    ds = make_ds([
        field("amount", "DOUBLE"),
        field("region", "VARCHAR"),
    ])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID, sample_limit=5)

    assert payload["row_count"] == 10
    assert payload["sampled"] is False
    assert payload["sample_size"] == 10
    assert isinstance(payload["generated_at"], str) and payload["generated_at"]

    by_name = {f["name"]: f for f in payload["fields"]}
    amount = by_name["amount"]
    assert amount["type"] == "DOUBLE"
    assert amount["null_rate"] == pytest.approx(0.2)
    assert amount["distinct_count"] == 5
    assert amount["min"] == 12.5 and amount["max"] == 99.0
    assert amount["sample_values"] == ["12.5", "99"]

    region = by_name["region"]
    # 字符串列的 MIN/MAX 是字典序，对分析无意义 —— 服务刻意不返回
    assert region["min"] is None and region["max"] is None
    assert region["sample_values"] == ["华东", "华南"]

    # 小表不应该走采样
    assert not any("USING SAMPLE" in q for q in fake.queries)
    assert all("USING SAMPLE" not in q for q in stat_queries(fake))


async def test_zero_row_table_reports_zero_null_rate(monkeypatch):
    use_fake_duck(monkeypatch, FakeDuck(row_count=0, stats={"amount": (0, 0, 0, None, None)}))
    ds = make_ds([field("amount", "DOUBLE")])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    assert payload["row_count"] == 0
    assert payload["sampled"] is False
    assert payload["fields"][0]["null_rate"] == 0.0


# ── 2. 超阈值 → 采样 ─────────────────────────────────────────────────

async def test_large_table_downgrades_to_sampling(monkeypatch):
    big = SAMPLE_ROW_THRESHOLD * 2 + 500_000
    fake = use_fake_duck(monkeypatch, FakeDuck(
        row_count=big,
        stats={"amount": (SAMPLE_ROWS, SAMPLE_ROWS, 42, 1.0, 900.0)},
        samples={"amount": ["1"]},
    ))
    ds = make_ds([field("amount", "DOUBLE")])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    # 真实行数必须如实上报（不能被采样数顶替）
    assert payload["row_count"] == big
    assert payload["sampled"] is True
    assert payload["sample_size"] == SAMPLE_ROWS
    assert payload["sample_size"] < payload["row_count"]

    # 关键断言：字段统计（含 COUNT(DISTINCT)）跑在采样集上，不是全表
    stats_sql = stat_queries(fake)
    assert stats_sql, "应当至少执行一条字段统计 SQL"
    assert all("USING SAMPLE" in q for q in stats_sql)
    assert payload["fields"][0]["distinct_count"] == 42


async def test_sampling_falls_back_to_limit_when_unsupported(monkeypatch):
    big = SAMPLE_ROW_THRESHOLD + 1
    fake = use_fake_duck(monkeypatch, FakeDuck(
        row_count=big,
        supports_sampling=False,
        stats={"amount": (SAMPLE_ROWS, SAMPLE_ROWS, 7, 0.0, 10.0)},
    ))
    ds = make_ds([field("amount", "DOUBLE")])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    assert payload["sampled"] is True
    assert payload["sample_size"] == SAMPLE_ROWS
    stats_sql = stat_queries(fake)
    assert stats_sql
    # 回退形态：FROM (SELECT * FROM t LIMIT n)
    assert all("(SELECT * FROM" in q and "LIMIT" in q for q in stats_sql)
    assert not any("USING SAMPLE" in q for q in stats_sql)


# ── 3. 敏感列 ────────────────────────────────────────────────────────

async def test_sensitive_column_has_statistics_but_no_sample_values(monkeypatch):
    fake = use_fake_duck(monkeypatch, FakeDuck(
        row_count=10,
        stats={"phone": (10, 9, 4, "138", "139")},
        samples={"phone": ["13800000000", "13900000000"]},
    ))
    ds = make_ds([field("phone", "VARCHAR", sensitive=True)])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    item = payload["fields"][0]
    assert item["sensitive"] is True
    assert item["sample_values"] == []
    # 统计量仍然给（这是画像的价值，且不泄漏原始值）
    assert item["null_rate"] == pytest.approx(0.1)
    assert item["distinct_count"] == 4
    # 绝不能为了样例值把敏感列的原始数据取回来
    assert not any('CAST("phone"' in q for q in fake.queries)


async def test_sensitive_column_detected_by_name_without_explicit_flag(monkeypatch):
    """schema_meta 没标 sensitive 时，按列名启发式识别（复用 sensitive_columns）。"""
    fake = use_fake_duck(monkeypatch, FakeDuck(
        row_count=5, stats={"user_password": (5, 5, 5, None, None)},
        samples={"user_password": ["hunter2"]},
    ))
    ds = make_ds([field("user_password", "VARCHAR")])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    item = payload["fields"][0]
    assert item["sensitive"] is True
    assert item["sample_values"] == []
    assert not any('CAST("user_password"' in q for q in fake.queries)


# ── 4. 字段过滤 ──────────────────────────────────────────────────────

async def test_fields_filter_keeps_requested_order_and_skips_others(monkeypatch):
    fake = use_fake_duck(monkeypatch, FakeDuck(row_count=10))
    ds = make_ds([
        field("amount", "DOUBLE"),
        field("region", "VARCHAR"),
        field("customer_name", "VARCHAR"),
    ])

    payload = await profile_datasource(
        FakeSession(ds), USER_ID, DS_ID, fields=["region", "amount"]
    )

    assert [f["name"] for f in payload["fields"]] == ["region", "amount"]
    assert len(stat_queries(fake)) == 2
    assert not any("customer_name" in q for q in fake.queries)


async def test_unknown_field_raises(monkeypatch):
    use_fake_duck(monkeypatch, FakeDuck(row_count=10))
    ds = make_ds([field("amount", "DOUBLE")])

    with pytest.raises(InvalidFieldError) as err:
        await profile_datasource(FakeSession(ds), USER_ID, DS_ID, fields=["amount", "nope"])

    assert "nope" in str(err.value)


async def test_too_many_requested_fields_raises(monkeypatch):
    use_fake_duck(monkeypatch, FakeDuck(row_count=10))
    ds = make_ds([field(f"c{i}", "VARCHAR") for i in range(MAX_PROFILE_FIELDS + 1)])

    with pytest.raises(InvalidFieldError):
        await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    # 显式指定（未超上限）时允许只画像其中一部分
    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID, fields=["c0", "c1"])
    assert len(payload["fields"]) == 2


# ── 5. 属主校验 ──────────────────────────────────────────────────────

async def test_non_owner_datasource_raises_not_found(monkeypatch):
    use_fake_duck(monkeypatch, FakeDuck(row_count=10))
    # 会话按 (id, user_id) 查不到 → 返回 None（等价于"别人的数据源"）
    session = FakeSession(None)

    with pytest.raises(DatasourceNotFoundError):
        await profile_datasource(session, OTHER_USER, DS_ID)


async def test_owner_lookup_queries_scoped_by_user(monkeypatch):
    """属性校验不是"取了再比较"，而是 SQL 条件里就带 user_id。"""
    fake = use_fake_duck(monkeypatch, FakeDuck(row_count=1))
    ds = make_ds([field("amount", "DOUBLE")])
    captured = {}

    class _RecordingSession:
        async def execute(self, stmt):
            captured["stmt"] = stmt
            return _FakeResult(ds)

    await profile_datasource(_RecordingSession(), USER_ID, DS_ID)

    sql = str(captured["stmt"])
    assert "user_id" in sql
    assert "datasources" in sql.lower()


# ── 6. 端点：404 / 字段参数 / 错误码映射 ────────────────────────────

def _endpoint():
    from app.api.v1 import datasources as ds_mod
    return ds_mod


async def test_endpoint_returns_404_for_non_owner():
    ds_mod = _endpoint()

    class _Service:
        async def get_by_id(self, datasource_id, user_id):  # noqa: ARG002
            return None

    with pytest.raises(HTTPException) as err:
        await ds_mod.profile_datasource_endpoint(
            datasource_id=DS_ID,
            fields=None,
            sample_limit=5,
            db=None,
            service=_Service(),
            current_user=SimpleNamespace(id=OTHER_USER),
        )

    assert err.value.status_code == 404
    assert err.value.detail["code"] == "NOT_FOUND"


async def test_endpoint_splits_fields_and_returns_payload():
    ds_mod = _endpoint()
    seen = {}

    async def _fake_profile(db, user_id, datasource_id, *, fields=None, sample_limit=5):  # noqa: ARG001
        seen["fields"] = fields
        seen["sample_limit"] = sample_limit
        return {"row_count": 1, "sampled": False, "sample_size": 1, "fields": [], "generated_at": "now"}

    class _Service:
        async def get_by_id(self, datasource_id, user_id):  # noqa: ARG002
            return SimpleNamespace(id=DS_ID)

    orig = ds_mod.profile_datasource
    ds_mod.profile_datasource = _fake_profile
    try:
        resp = await ds_mod.profile_datasource_endpoint(
            datasource_id=DS_ID,
            fields="amount, region ,",
            sample_limit=3,
            db=None,
            service=_Service(),
            current_user=SimpleNamespace(id=USER_ID),
        )
    finally:
        ds_mod.profile_datasource = orig

    assert seen["fields"] == ["amount", "region"]
    assert seen["sample_limit"] == 3
    assert resp.data["row_count"] == 1


async def test_endpoint_maps_invalid_field_to_400_and_attach_failure_to_502():
    ds_mod = _endpoint()

    class _Service:
        async def get_by_id(self, datasource_id, user_id):  # noqa: ARG002
            return SimpleNamespace(id=DS_ID)

    async def _raise(exc):
        def _fake(db, user_id, datasource_id, *, fields=None, sample_limit=5):  # noqa: ARG001
            raise exc
        return _fake

    orig = ds_mod.profile_datasource
    try:
        ds_mod.profile_datasource = await _raise(InvalidFieldError("字段不存在：nope"))
        with pytest.raises(HTTPException) as err:
            await ds_mod.profile_datasource_endpoint(
                datasource_id=DS_ID, fields="nope", sample_limit=5, db=None,
                service=_Service(), current_user=SimpleNamespace(id=USER_ID),
            )
        assert err.value.status_code == 400
        assert err.value.detail["code"] == "INVALID_FIELD"

        ds_mod.profile_datasource = await _raise(
            DatasourceProfileError("not attached", code="ATTACH_FAILED")
        )
        with pytest.raises(HTTPException) as err2:
            await ds_mod.profile_datasource_endpoint(
                datasource_id=DS_ID, fields=None, sample_limit=5, db=None,
                service=_Service(), current_user=SimpleNamespace(id=USER_ID),
            )
        assert err2.value.status_code == 502
        assert err2.value.detail["code"] == "ATTACH_FAILED"
    finally:
        ds_mod.profile_datasource = orig


# ── 7. 单列失败隔离 ──────────────────────────────────────────────────

async def test_one_field_failure_does_not_break_others(monkeypatch):
    fake = use_fake_duck(monkeypatch, FakeDuck(
        row_count=10,
        stats={"amount": (10, 10, 5, 1.0, 9.0)},
        fail_fields={"weird"},
    ))
    ds = make_ds([field("weird", "STRUCT"), field("amount", "DOUBLE")])

    payload = await profile_datasource(FakeSession(ds), USER_ID, DS_ID)

    by_name = {f["name"]: f for f in payload["fields"]}
    assert "error" in by_name["weird"]
    assert by_name["weird"]["null_rate"] is None
    assert by_name["amount"]["distinct_count"] == 5
    assert by_name["amount"]["max"] == 9.0