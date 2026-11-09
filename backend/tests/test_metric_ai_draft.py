# -*- coding: utf-8 -*-
"""指标中心「AI 起草」接口（POST /metrics/draft）的回归测试。

覆盖三块：
1. 正常草稿：LLM 输出 JSON → 三道校验通过 → 响应字段齐全（camelCase）→ 试算给出 sampleValue；
2. 校验：非法公式 / 未知字段 / SUM 用在文本列 / key 冲突 / 占位符 —— 都是 **HTTP 200 + errors**；
3. 容错：markdown 围栏能解析、非法 JSON 重试一次、AI 不可用映射 503、数据源缺失 404。
另外钉两件事：**草稿绝不写库**（不调 add/commit）、**路由声明顺序** /draft 在 /{metric_id} 之前。

真模型一律 monkeypatch（不联网、不依赖音 DB）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.config import settings
from app.schemas import MetricDraftRequest
from app.services import metric_ai_service as svc
from app.services.llm_client import AINotConfiguredError, AIUpstreamError
from app.api.v1 import metrics as metrics_api

USER_ID = uuid4()
DS_ID = uuid4()

FIELDS = [
    {"name": "amount", "data_type": "DOUBLE", "category": "measure", "sample": [12.5, 30]},
    {"name": "status", "data_type": "VARCHAR", "category": "dimension", "sample": ["paid"]},
    {"name": "order_date", "data_type": "DATE", "category": "dimension", "sample": ["2026-01-01"]},
    {"name": "customer_id", "data_type": "BIGINT", "category": "dimension"},
    # 敏感列：schema 同步时 sample 已被清空，这里故意留一个原始值，验证渲染层也不会外泄
    {"name": "card_no", "data_type": "VARCHAR", "sensitive": True, "sample": ["6222 0000"]},
]

GOOD_DRAFT = {
    "key": "paid_order_amount",
    "name": "已支付订单销售额",
    "description": "已支付订单的 amount 求和，含税，不含退款",
    "formula": "SUM(CASE WHEN \"status\" = 'paid' THEN \"amount\" ELSE 0 END)",
    "agg_kind": "SUM",
    "ambiguities": ["是否剔除退款订单未明确，默认不剔除"],
}


# ── 假件 ──────────────────────────────────────────────────────────────────

def _datasource(fields=None):
    return SimpleNamespace(
        id=DS_ID,
        name="销售库",
        source_type=SimpleNamespace(value="csv"),
        user_id=USER_ID,
        created_at=None,
        schema_meta={
            "fields": list(FIELDS if fields is None else fields),
            "table_name": "data",
        },
    )


def _result(*, scalar_one_or_none=None, scalars=None, rows=None):
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar_one_or_none
    m.scalars.return_value = MagicMock(all=MagicMock(return_value=scalars or []))
    m.all.return_value = rows if rows is not None else []
    return m


class FakeDB:
    """按 SQL 文本分流：datasources 查询 → 数据源；其余 → 既有指标。

    另外记录 add/commit 调用次数，用来断言"草稿绝不写库"。
    """

    def __init__(self, *, datasource="__default__", metrics=None):
        self.datasource = _datasource() if datasource == "__default__" else datasource
        self.metrics = (
            metrics
            if metrics is not None
            else [SimpleNamespace(key="sales_amount", name="销售额", formula='SUM("amount")')]
        )
        self.executed: list[str] = []
        self.add = MagicMock()
        self.commit = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):
        text = str(stmt).lower()
        self.executed.append(text)
        if "datasources" in text:
            return _result(scalar_one_or_none=self.datasource)
        return _result(rows=self.metrics)


class FakeLLM:
    def __init__(self, replies=None, error=None):
        self.replies = list(replies or [])
        self.error = error
        self.calls: list[dict] = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.replies.pop(0)


def _patch_llm(monkeypatch, *, replies=None, error=None) -> FakeLLM:
    llm = FakeLLM(replies=replies, error=error)
    monkeypatch.setattr(svc, "LLMClient", lambda *a, **k: llm)
    return llm


def _patch_trial(monkeypatch, *, value=123.0, error=None):
    """试算链路：默认返回一个带数值的结果；给 error 时模拟执行失败。"""
    calls = {"n": 0}

    async def fake_execute(**kwargs):
        calls["n"] += 1
        if error is not None:
            raise error
        return MagicMock(rows=[{"paid_order_amount": value}], columns=["paid_order_amount"])

    monkeypatch.setattr(svc, "execute_chart_query", fake_execute)
    return calls


async def _call(db=None, *, prompt=u"已支付订单的销售额", datasource_id=None, table_ref=None):
    body = MetricDraftRequest(
        prompt=prompt, datasource_id=datasource_id, table_ref=table_ref
    )
    return await metrics_api.draft_metric_ai(
        body=body, current_user=SimpleNamespace(id=USER_ID), db=db or FakeDB()
    )


# ── 1. 正常草稿 ───────────────────────────────────────────────────────────

async def test_draft_ok_returns_full_camelcase_payload(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps(GOOD_DRAFT, ensure_ascii=False)])
    trial = _patch_trial(monkeypatch, value=123.0)
    db = FakeDB()

    resp = await _call(db)

    data = resp.data
    assert data["validation"]["ok"] is True, data["validation"]
    assert data["key"] == "paid_order_amount"
    assert data["name"] == "已支付订单销售额"
    assert data["formula"].startswith("SUM(CASE WHEN")
    assert data["aggKind"] == "SUM"
    assert data["datasourceId"] == str(DS_ID)
    assert data["tableRef"] == "data"
    assert data["sampleValue"] == 123.0
    assert data["sampleError"] is None
    assert data["model"] == settings.openai_model
    assert data["ambiguities"] and "退款" in data["ambiguities"][0]
    # references 由后端从公式解析（不采信模型自述）
    fields = {r["field"] for r in data["references"]}
    assert fields == {"status", "amount"}
    # 条件分支里的列标 FILTER（它决定"哪些行进聚合"），取值分支才标聚合方式
    assert {r["field"]: r["transform"] for r in data["references"]} == {
        "amount": "SUM",
        "status": "FILTER",
    }
    assert trial["n"] == 1, "校验通过应跑一次试算"
    # 绝不写库
    db.add.assert_not_called()
    db.commit.assert_not_called()


async def test_llm_is_called_in_json_mode(monkeypatch):
    llm = _patch_llm(monkeypatch, replies=[json.dumps(GOOD_DRAFT)])
    _patch_trial(monkeypatch)
    await _call()
    kwargs = llm.calls[0]["kwargs"]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["enable_thinking"] is False
    assert kwargs["max_tokens"] == 900
    assert kwargs["temperature"] == 0.2
    # system 里必须写清边界（否则模型会写子查询/窗口）
    system_text = llm.calls[0]["messages"][0]["content"]
    for token in ("子查询", "窗口", "CASE WHEN", "ambiguities", "JSON"):
        assert token in system_text, f"系统提示词缺关键约束：{token}"


# ── 2. 三道校验（都是 200 + errors）────────────────────────────────────────

async def test_invalid_formula_returns_200_with_errors_and_skips_trial(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps({**GOOD_DRAFT, "formula": "(SELECT 1)"})])
    trial = _patch_trial(monkeypatch)

    data = (await _call()).data

    assert data["validation"]["ok"] is False
    assert any(u"公式不合法" in e for e in data["validation"]["errors"])
    assert trial["n"] == 0, "校验不通过的公式不该拿去试算"


async def test_unknown_field_is_rejected(monkeypatch):
    _patch_llm(
        monkeypatch,
        replies=[json.dumps({**GOOD_DRAFT, "formula": 'SUM("monetary_value")'})],
    )
    _patch_trial(monkeypatch)

    data = (await _call()).data

    errors = data["validation"]["errors"]
    assert any(u"不存在的字段" in e and "monetary_value" in e for e in errors), errors


async def test_numeric_agg_on_text_field_is_rejected(monkeypatch):
    _patch_llm(
        monkeypatch, replies=[json.dumps({**GOOD_DRAFT, "formula": 'SUM("status")'})]
    )
    _patch_trial(monkeypatch)

    data = (await _call()).data

    errors = data["validation"]["errors"]
    assert any("SUM" in e and u"数值" in e for e in errors), errors


async def test_key_conflict_is_rejected_without_renaming(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps({**GOOD_DRAFT, "key": "sales_amount"})])
    _patch_trial(monkeypatch)

    data = (await _call()).data

    errors = data["validation"]["errors"]
    assert any(u"已存在" in e for e in errors), errors
    # 不自动改名：草稿 key 原样返回，交给前端提示人改
    assert data["key"] == "sales_amount"


async def test_key_format_is_validated(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps({**GOOD_DRAFT, "key": "Paid Amount"})])
    _patch_trial(monkeypatch)

    data = (await _call()).data

    assert any("key" in e for e in data["validation"]["errors"])


async def test_placeholder_in_formula_is_rejected(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps({**GOOD_DRAFT, "formula": "SUM({{amount}})"})])
    _patch_trial(monkeypatch)

    data = (await _call()).data

    assert any(u"占位符" in e for e in data["validation"]["errors"])


async def test_missing_description_is_a_warning_not_error(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps({**GOOD_DRAFT, "description": ""})])
    _patch_trial(monkeypatch)

    data = (await _call()).data

    assert data["validation"]["ok"] is True
    assert any(u"口径" in w for w in data["validation"]["warnings"])


# ── 3. 容错与错误映射 ─────────────────────────────────────────────────────

async def test_markdown_fenced_json_is_parsed(monkeypatch):
    fence = chr(96) * 3
    fenced = fence + "json\n" + json.dumps(GOOD_DRAFT, ensure_ascii=False) + "\n" + fence
    _patch_llm(monkeypatch, replies=[fenced])
    _patch_trial(monkeypatch)

    data = (await _call()).data

    assert data["validation"]["ok"] is True
    assert data["key"] == "paid_order_amount"


async def test_invalid_json_retries_once_then_returns_errors(monkeypatch):
    llm = _patch_llm(monkeypatch, replies=["这不是 JSON", "还不是 JSON"])
    _patch_trial(monkeypatch)

    data = (await _call()).data

    assert data["validation"]["ok"] is False
    assert any(u"不是合法 JSON" in e for e in data["validation"]["errors"])
    assert len(llm.calls) == 2, "解析失败必须重试一次（把错误回灌模型）"
    # 重试时把上一次输出与纠错指令带回去
    retry_messages = llm.calls[1]["messages"]
    assert any(m["role"] == "assistant" for m in retry_messages)


async def test_ai_upstream_error_maps_to_503(monkeypatch):
    _patch_llm(monkeypatch, error=AIUpstreamError("upstream 500"))
    _patch_trial(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _call()

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "AI_UNAVAILABLE"


async def test_ai_not_configured_maps_to_503(monkeypatch):
    _patch_llm(monkeypatch, error=AINotConfiguredError("no key"))

    with pytest.raises(HTTPException) as exc:
        await _call()

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "AI_UNAVAILABLE"


async def test_missing_datasource_returns_404(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps(GOOD_DRAFT)])
    db = FakeDB(datasource=None)

    with pytest.raises(HTTPException) as exc:
        await _call(db, datasource_id=uuid4())

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "DATASOURCE_NOT_FOUND"


async def test_schema_unavailable_returns_400(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps(GOOD_DRAFT)])
    db = FakeDB(datasource=_datasource(fields=[]))

    with pytest.raises(HTTPException) as exc:
        await _call(db)

    assert exc.value.status_code == 400
    assert u"字段尚未同步" in exc.value.detail["message"]


async def test_trial_failure_only_sets_sample_error(monkeypatch):
    _patch_llm(monkeypatch, replies=[json.dumps(GOOD_DRAFT)])
    _patch_trial(monkeypatch, error=RuntimeError("duckdb down"))

    data = (await _call()).data

    assert data["validation"]["ok"] is True, "试算失败不影响草稿本身"
    assert data["sampleValue"] is None
    assert data["sampleError"] and "duckdb down" in data["sampleError"]


# ── 4. 上下文装配（省 token + 不泄敏）────────────────────────────────────

def test_schema_block_skips_sensitive_samples_and_caps_fields():
    block = svc._render_schema_block(_datasource(), FIELDS)
    assert '"amount" (DOUBLE, numeric)' in block
    assert "12.5" in block
    assert u"敏感" in block and "6222 0000" not in block, "敏感列样例绝不能进 prompt"

    many = [
        {"name": f"c{i}", "data_type": "INT", "sample": [i]} for i in range(50)
    ]
    capped = svc._render_schema_block(_datasource(), many)
    assert capped.count("- \"c") == svc.MAX_CONTEXT_FIELDS
    assert u"其余 10 个字段未展示" in capped


def test_prompt_includes_existing_metric_keys_and_schema():
    rows = [SimpleNamespace(key="sales_amount", name=u"销售额", formula='SUM("amount")')]
    messages = svc.build_draft_messages(
        prompt=u"已支付订单的销售额",
        schema_block=svc._render_schema_block(_datasource(), FIELDS),
        metrics_block=svc._render_metrics_block(rows),
    )
    user_text = messages[1]["content"]
    assert "sales_amount" in user_text
    assert u"已支付订单的销售额" in user_text
    assert '"amount"' in user_text


def test_references_attribute_to_aggregate_not_case():
    refs = svc.extract_references(
        "SUM(CASE WHEN \"status\" = 'paid' THEN \"amount\" ELSE 0 END)"
    )
    by_field = {r["field"]: r["transform"] for r in refs}
    # 血缘不标成 CASE/IF（那是表达式结构）；条件分支标 FILTER，取值分支标聚合
    assert by_field == {"status": "FILTER", "amount": "SUM"}


def test_metric_reference_is_not_treated_as_field():
    formula = 'metric("sales_amount") * 1.2'
    assert svc.extract_formula_fields(formula) == []
    refs = svc.extract_references(formula)
    assert refs == [{"field": "sales_amount", "transform": "METRIC"}]


# ── 5. 路由顺序（/draft 必须在 /{metric_id} 之前）──────────────────────────

def test_draft_route_declared_before_metric_id_route():
    paths = [getattr(r, "path", "") for r in metrics_api.router.routes]
    assert "/metrics/draft" in paths
    assert paths.index("/metrics/draft") < paths.index("/metrics/{metric_id}"), (
        "路由顺序错了：/{metric_id} 会把 /metrics/draft 当成 metric_id"
    )
