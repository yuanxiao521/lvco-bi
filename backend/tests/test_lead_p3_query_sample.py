# -*- coding: utf-8 -*-
"""P3 观察工具补线：query_sample 真实取 5 行样本。

任务书 §2.2 把 query_sample 列为观察类（只读）能力之一，§6 不变量 1/4 要求：
  · 禁 SELECT *（列名显式枚举）；
  · 不另写执行器（复用 query_sql 的 guard/归属校验/超时链路）；
  · 失败即降级（取不到返回 None 并记 degraded，绝不返回伪造样本）。

这里锁住三件事：
  1. sample_sql 是纯函数——列名逐个加双引号、转义内嵌引号、带 LIMIT，结构上无 *；
  2. 取数链路真的走了既有执行工具（把 SQL 交给它，而不是自己连库）；
  3. 各种失败形态的降级原因都在（数据源不存在 / 无字段 / 查询报错 / 空结果）。
"""
from __future__ import annotations

import json

from app.services.agents.lead import lead_observe
from app.services.agents.lead.lead_observe import query_sample, sample_sql

# ── 纯函数：SQL 构造 ────────────────────────────────────────────────────────

def test_sample_sql_uses_explicit_columns_and_limit():
    sql = sample_sql('"sch"."data"', ["region", "amount"])
    assert sql == 'SELECT "region", "amount" FROM "sch"."data" LIMIT 5'
    assert "*" not in sql.replace("LIMIT", "")   # 结构上不可能出现 SELECT *
    assert sql.endswith("LIMIT 5")


def test_sample_sql_escapes_embedded_quotes():
    sql = sample_sql('"sch"."data"', ['we"ird'])
    assert '"we""ird"' in sql            # 双写转义，不破坏语句结构


def test_sample_sql_honours_custom_limit():
    assert sample_sql('"s"."data"', ["a"], limit=3).endswith("LIMIT 3")


# ── 取数链路：复用 query_sql，不自己连库 ────────────────────────────────────

class _FakeTool:
    """替身：只记录被调用时的 SQL，返回固定 payload。"""

    calls: list[dict] = []
    payload: dict = {}

    async def execute(self, **kwargs):
        _FakeTool.calls.append(kwargs)
        return json.dumps(_FakeTool.payload, ensure_ascii=False, default=str)


def _ds(fields=None, table_ref='"sch"."data"', ds_id="ds-1"):
    """与 lead_tools._load_available_datasources 的返回形状一致（dict 列表）。"""
    return {
        "id": ds_id,
        "fields": [{"name": "region"}, {"name": "amount"}] if fields is None else fields,
        "table_ref": table_ref,
    }


async def _stub_datasources(monkeypatch, payload):
    async def _fake(db_session, user_id, *, datasource_id=None, inject_fields_unselected=False):
        _ = (db_session, user_id, datasource_id, inject_fields_unselected)
        return payload
    monkeypatch.setattr(lead_observe, "list_datasources", _fake)


async def test_query_sample_runs_through_existing_query_tool(monkeypatch):
    """真实取数：SQL 交给 QueryDatasourceTool（guard/归属/超时都在那条链路上）。"""
    from app.services import agent_tools

    _FakeTool.calls = []
    _FakeTool.payload = {
        "columns": ["region", "amount"],
        "row_count": 2,
        "rows": [["华东", 120.5], ["华北", None]],
    }
    await _stub_datasources(monkeypatch, [_ds()])
    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)

    degradation: list[str] = []
    raw = await query_sample(None, "u1", "ds-1", degradation=degradation)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["columns"] == ["region", "amount"]
    assert payload["row_count"] == 2
    # 行被归一成 {列名: 值}，便于模型直接读
    assert payload["rows"][0] == {"region": "华东", "amount": 120.5}
    assert payload["rows"][1] == {"region": "华北", "amount": None}
    assert degradation == []
    # 执行的是我们构造的样本 SQL，而不是别的取数语句
    sent_sql = _FakeTool.calls[0]["sql"]
    assert sent_sql == 'SELECT "region", "amount" FROM "sch"."data" LIMIT 5'
    assert _FakeTool.calls[0]["datasource_id"] == "ds-1"


async def test_query_sample_caps_wide_tables(monkeypatch):
    """宽表只取前 12 列：样本用于认形态，不是导出全表。"""
    from app.services import agent_tools

    wide = [{"name": "c" + str(i)} for i in range(30)]
    _FakeTool.calls = []
    _FakeTool.payload = {"columns": [f["name"] for f in wide], "rows": [[i for i in range(30)]]}
    await _stub_datasources(monkeypatch, [_ds(wide)])
    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)

    payload = json.loads(await query_sample(None, "u1", "ds-1"))
    sent_sql = _FakeTool.calls[0]["sql"]
    assert "c11" in sent_sql and "c12" not in sent_sql
    assert len(payload["columns"]) == lead_observe._SAMPLE_MAX_FIELDS


async def test_query_sample_truncates_long_cells(monkeypatch):
    from app.services import agent_tools

    _FakeTool.calls = []
    _FakeTool.payload = {"columns": ["note"], "rows": [["x" * 200]]}
    await _stub_datasources(monkeypatch, [_ds()])
    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)

    payload = json.loads(await query_sample(None, "u1", "ds-1"))
    cell = payload["rows"][0]["note"]
    assert cell.endswith("…") and len(cell) == lead_observe._SAMPLE_CELL_CHARS + 1


# ── 失败即降级 ─────────────────────────────────────────────────────────────

async def test_query_sample_degrades_when_datasource_missing(monkeypatch):
    """点名了不存在的源：不退回别的源（会用错数据），直接降级。"""
    await _stub_datasources(monkeypatch, [])
    degradation: list[str] = []
    got = await query_sample(None, "u1", "nope", degradation=degradation)
    assert got is None
    assert "lead_agent_observe_query_sample_datasource_not_found" in degradation


async def test_query_sample_degrades_without_fields(monkeypatch):
    await _stub_datasources(monkeypatch, [_ds([])])
    degradation: list[str] = []
    got = await query_sample(None, "u1", "ds-1", degradation=degradation)
    assert got is None
    assert "lead_agent_observe_query_sample_no_fields" in degradation


async def test_query_sample_returns_readable_error_on_guard_block(monkeypatch):
    """查询报错时返回可读错误（不是 None）：模型看得到原因才会自纠。"""
    from app.services import agent_tools

    _FakeTool.calls = []
    _FakeTool.payload = {"error": "禁止访问未授权的 schema", "hint": 'FROM 子句必须用: "sch"."data"'}
    await _stub_datasources(monkeypatch, [_ds()])
    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)

    degradation: list[str] = []
    payload = json.loads(await query_sample(None, "u1", "ds-1", degradation=degradation))
    assert payload["ok"] is False
    assert "禁止访问未授权的 schema" in payload["error"]
    assert payload["hint"]
    assert "lead_agent_observe_query_sample_query_error" in degradation


async def test_query_sample_degrades_on_empty_result(monkeypatch):
    from app.services import agent_tools

    _FakeTool.calls = []
    _FakeTool.payload = {"columns": ["a"], "rows": []}
    await _stub_datasources(monkeypatch, [_ds()])
    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)

    degradation: list[str] = []
    payload = json.loads(await query_sample(None, "u1", "ds-1", degradation=degradation))
    assert payload["ok"] is False
    assert "lead_agent_observe_query_sample_empty" in degradation


async def test_recon_tool_routes_query_sample(monkeypatch):
    """recon_tool 的分派把 query_sample 接到真实实现（不再是 unimplemented 桩）。"""
    from app.services import agent_tools

    _FakeTool.calls = []
    _FakeTool.payload = {"columns": ["a"], "rows": [[1]]}
    await _stub_datasources(monkeypatch, [_ds()])
    monkeypatch.setattr(agent_tools, "QueryDatasourceTool", _FakeTool)

    class _Ctx:
        entry = "chat"
        user_id = "u1"
        datasource_id = "ds-1"
        canvas_state = None
        metrics_ctx = ""

    degradation: list[str] = []
    got = await lead_observe.recon_tool(
        "query_sample", ctx=_Ctx(), args={}, db_session=None, degradation=degradation,
    )
    assert got is not None and json.loads(got)["ok"] is True
    assert not any("unimplemented" in d for d in degradation)