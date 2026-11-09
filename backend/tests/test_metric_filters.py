"""指标级范围口径（metric_filters）：CRUD / 校验 / 编译 / AI 起草 / 级联。

口径 = 聚合 + 过滤：filter 编译进 WHERE 且排在调用方 filters 之前
（指标口径先收窄，临时过滤继续收紧）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

from app.models.metric import MetricDefinition
from app.services.metric_service import (
    filters_to_dicts,
    resolve_metric,
    validate_metric_filter_items,
)

MID = uuid4()
USER_ID = uuid4()


def make_metric(**over) -> MetricDefinition:
    base = dict(
        id=MID,
        key="paid_sales",
        name="已支付销售额",
        formula="SUM(CASE WHEN \"status\" = 'paid' THEN \"amount\" ELSE 0 END)",
        formula_type="basic",
        version=1,
        agg_kind="SUM",
        user_id=USER_ID,
        active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(over)
    return MetricDefinition(**base)


def make_filter(field="status", op="eq", value="paid"):
    return MagicMock(field=field, op=op, value=value)


# ── 校验 ─────────────────────────────────────────────────────────────────

def test_op_whitelist_rejects_unknown():
    errors, _ = validate_metric_filter_items([{"field": "status", "op": "regex", "value": "x"}])
    assert any("不支持的操作符" in e for e in errors)


def test_op_whitelist_accepts_all_documented():
    for op in ["eq", "neq", "gt", "gte", "lt", "lte", "like"]:
        errors, _ = validate_metric_filter_items([{"field": "f", "op": op, "value": 1}])
        assert not errors, (op, errors)
    errors, _ = validate_metric_filter_items([{"field": "f", "op": "in", "value": [1]}])
    assert not errors
    errors, _ = validate_metric_filter_items([{"field": "f", "op": "between", "value": [1, 2]}])
    assert not errors
    for op in ["isnull", "isnotnull"]:
        errors, _ = validate_metric_filter_items([{"field": "f", "op": op, "value": True}])
        assert not errors, (op, errors)


def test_in_requires_nonempty_list():
    errors, _ = validate_metric_filter_items([{"field": "region", "op": "in", "value": []}])
    assert any("非空数组" in e for e in errors)


def test_between_requires_pair():
    errors, _ = validate_metric_filter_items([{"field": "amount", "op": "between", "value": [1]}])
    assert any("lo, hi" in e for e in errors)


def test_scalar_op_rejects_list_value():
    errors, _ = validate_metric_filter_items([{"field": "region", "op": "eq", "value": ["a"]}])
    assert any("标量" in e for e in errors)


def test_max_20_filters():
    items = [{"field": "f", "op": "eq", "value": 1} for _ in range(21)]
    errors, _ = validate_metric_filter_items(items)
    assert any("最多 20 条" in e for e in errors)


# ── 编译：resolve_metric 把 filters 排在最前 ────────────────────────────

def test_resolve_metric_prepends_metric_filters():
    m = make_metric()
    m.filters = [make_filter("status", "eq", "paid")]
    cfg = resolve_metric(m, dimensions=["region"], filters=[{"field": "region", "op": "eq", "value": "华东"}])
    assert cfg.filters[0].field == "status"   # 指标口径在前
    assert cfg.filters[-1].field == "region"  # 调用方的在后


def test_resolve_metric_without_filters_unchanged():
    m = make_metric()
    m.filters = []
    cfg = resolve_metric(m, filters=[{"field": "region", "op": "eq", "value": "华东"}])
    assert len(cfg.filters) == 1 and cfg.filters[0].field == "region"


def test_resolve_metric_expression_untouched_by_filters():
    """filter 不改变公式表达式本身（口径 = 公式 + 范围，二者正交）。"""
    m1 = make_metric()
    m1.filters = []
    m2 = make_metric()
    m2.filters = [make_filter()]
    e1 = resolve_metric(m1).measures[0].expression
    e2 = resolve_metric(m2).measures[0].expression
    assert e1 == e2


def test_filters_to_dicts_serializable():
    rows = [make_filter("status", "eq", "paid"), make_filter("amount", "between", [1, 2])]
    out = filters_to_dicts(rows)
    assert out == [{"field": "status", "op": "eq", "value": "paid"},
                   {"field": "amount", "op": "between", "value": [1, 2]}]


# ── AI 起草：filters 产出与校验 ─────────────────────────────────────────

def test_validate_draft_keeps_filters_and_checks_fields():
    from app.services import metric_ai_service as svc
    draft = {
        "key": "paid_sales", "name": "已支付销售额",
        "formula": 'SUM("amount")', "agg_kind": "SUM",
        "description": "仅已支付", "filters": [{"field": "status", "op": "eq", "value": "paid"}],
    }
    errors, _ = svc.validate_draft(draft, field_types={"amount": "numeric", "status": "other"}, existing_keys=set())
    assert not errors
    assert draft["filters"] == [{"field": "status", "op": "eq", "value": "paid"}]


def test_validate_draft_rejects_unknown_filter_field():
    from app.services import metric_ai_service as svc
    draft = {
        "key": "paid_sales", "name": "x", "formula": 'SUM("amount")', "agg_kind": "SUM",
        "description": "x", "filters": [{"field": "no_such_col", "op": "eq", "value": 1}],
    }
    errors, _ = svc.validate_draft(draft, field_types={"amount": "numeric"}, existing_keys=set())
    # 断言锚定 ASCII 字段名，避免 Windows 下断言重写编码不一致
    assert any("no_such_col" in e for e in errors)


def test_validate_draft_rejects_bad_filter_op():
    from app.services import metric_ai_service as svc
    draft = {
        "key": "paid_sales", "name": "x", "formula": 'SUM("amount")', "agg_kind": "SUM",
        "description": "x", "filters": [{"field": "status", "op": "regex", "value": "x"}],
    }
    errors, _ = svc.validate_draft(draft, field_types={"amount": "numeric"}, existing_keys=set())
    assert any("不支持的操作符" in e for e in errors)


def test_normalize_draft_defaults_filters_to_empty():
    from app.services.metric_ai_service import _normalize_draft
    d = _normalize_draft({"key": "k", "name": "n", "formula": "SUM(x)"})
    assert d["filters"] == []
