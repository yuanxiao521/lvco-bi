"""ToolExecutor 成功态 memo 专项测试：
1. query_sql 同参数第二次调用命中 memo（任务内去重）
2. error 结果不写入 memo（自纠错不被旧错误锚定）
3. 幂等工具（idempotent）保持无条件缓存（含错误）
"""
from __future__ import annotations

import json
from unittest.mock import patch

from app.services.agents.tool_executor import ToolExecutor


class FakeQueryTool:
    """假 query_sql：记录调用次数，可切换成功/失败。"""

    calls = 0
    fail = False

    async def execute(self, **kwargs) -> str:
        FakeQueryTool.calls += 1
        if FakeQueryTool.fail:
            return json.dumps({"error": "query failed", "hint": "check columns"})
        return json.dumps({"columns": ["a"], "rows": [{"a": 1}]})


def _mk_call(sql: str, i: int) -> dict:
    return {"id": f"call_{i}", "name": "query_sql", "arguments": json.dumps({"sql": sql})}


def _mk_executor(**kw) -> ToolExecutor:
    base = dict(user_id="u1", db_session=None, memo={}, success_cached_tools=frozenset({"query_sql"}))
    base.update(kw)
    return ToolExecutor(**base)


async def test_success_cached_tool_reuses_on_same_sql():
    """同 SQL 第二次调用命中 memo，工具只真执行一次。"""
    FakeQueryTool.calls = 0
    FakeQueryTool.fail = False
    exc = _mk_executor()
    tc1 = _mk_call("SELECT 1", 1)
    tc2 = _mk_call("SELECT 1", 2)  # 同 SQL
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeQueryTool()):
        r1 = await exc.execute_tool_call(tc1)
        r2 = await exc.execute_tool_call(tc2)
    assert r1.memo_hit is False
    assert r2.memo_hit is True, "同 SQL 第二次应命中 memo"
    assert FakeQueryTool.calls == 1, "工具应只真执行一次"


async def test_success_cached_tool_distinguishes_sql():
    """不同 SQL 不共享 memo。"""
    FakeQueryTool.calls = 0
    FakeQueryTool.fail = False
    exc = _mk_executor()
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeQueryTool()):
        await exc.execute_tool_call(_mk_call("SELECT 1", 1))
        await exc.execute_tool_call(_mk_call("SELECT 2", 2))
    assert FakeQueryTool.calls == 2


async def test_success_cached_tool_does_not_cache_errors():
    """error 结果不写 memo：重试同 SQL 会再次真执行，memo 保持为空（自纠错不被锚定）。"""
    FakeQueryTool.calls = 0
    FakeQueryTool.fail = True
    exc = _mk_executor()
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeQueryTool()):
        r1 = await exc.execute_tool_call(_mk_call("SELECT bad_col", 1))
        r2 = await exc.execute_tool_call(_mk_call("SELECT bad_col", 2))
    assert r1.is_error and r2.is_error
    assert FakeQueryTool.calls == 2, "error 不应命中 memo，重试应真执行"
    assert exc.memo == {}, "error 结果不应写入 memo"


# ── 执行器级上下文注入：canvas_id 由系统绑定，不依赖 LLM 填写 ──────────────

class FakeCanvasLayoutTool:
    """假 get_canvas_layout：记录实际收到的 canvas_id。"""

    seen: dict = {}

    async def execute(self, user_id: str = "", db_session=None, canvas_id: str = "", **kwargs) -> str:
        FakeCanvasLayoutTool.seen = {"canvas_id": canvas_id, "user_id": user_id}
        return json.dumps({"ok": True, "layout": "..."})


class FakeFixedSignatureTool:
    """签名固定的假工具（无 **kwargs、无 canvas_id）：不应被塞进上下文参数。"""

    seen: dict = {}

    async def execute(self, user_id: str = "", db_session=None, title: str = "") -> str:
        FakeFixedSignatureTool.seen = {"title": title}
        return json.dumps({"ok": True})


async def test_context_injects_canvas_id_when_llm_omits():
    """LLM 空参调用 get_canvas_layout 时，执行器自动补 canvas_id（回归：此前必报缺少画布上下文）。"""
    FakeCanvasLayoutTool.seen = {}
    exc = ToolExecutor(user_id="u1", db_session=None, context={"canvas_id": "cv-123"})
    tc = {"id": "call_1", "name": "get_canvas_layout", "arguments": "{}"}
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeCanvasLayoutTool()):
        r = await exc.execute_tool_call(tc)
    assert not r.is_error
    assert FakeCanvasLayoutTool.seen["canvas_id"] == "cv-123"
    assert FakeCanvasLayoutTool.seen["user_id"] == "u1"


async def test_context_does_not_override_explicit_llm_arg():
    """LLM 显式传入的 canvas_id 优先于上下文（支持读另一张画布）。"""
    FakeCanvasLayoutTool.seen = {}
    exc = ToolExecutor(user_id="u1", db_session=None, context={"canvas_id": "cv-123"})
    tc = {"id": "call_1", "name": "get_canvas_layout", "arguments": json.dumps({"canvas_id": "cv-999"})}
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeCanvasLayoutTool()):
        await exc.execute_tool_call(tc)
    assert FakeCanvasLayoutTool.seen["canvas_id"] == "cv-999"


async def test_context_skipped_for_fixed_signature_tool():
    """签名不接受该参数的工具不被注入，避免 TypeError。"""
    FakeFixedSignatureTool.seen = {}
    exc = ToolExecutor(user_id="u1", db_session=None, context={"canvas_id": "cv-123"})
    tc = {"id": "call_1", "name": "some_fixed_tool", "arguments": json.dumps({"title": "t"})}
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeFixedSignatureTool()):
        r = await exc.execute_tool_call(tc)
    assert not r.is_error, f"不应因注入而抛错: {r.result}"
    assert FakeFixedSignatureTool.seen == {"title": "t"}


class FakeLedgerAwareTool:
    """假画布工具（**kwargs）：记录是否收到台账对象。"""

    seen: dict = {}

    async def execute(self, user_id: str = "", db_session=None, **kwargs) -> str:
        FakeLedgerAwareTool.seen = {"ledger": kwargs.get("canvas_ledger"), "canvas_id": kwargs.get("canvas_id")}
        return json.dumps({"ok": True})


async def test_context_injects_canvas_ledger_instance():
    """台账对象随上下文注入画布工具（每个 LLM 轮次新建执行器，台账必须能传进去）。"""
    from app.services.canvas_tools import CanvasActionLedger

    FakeLedgerAwareTool.seen = {}
    ledger = CanvasActionLedger()
    exc = ToolExecutor(user_id="u1", db_session=None,
                       context={"canvas_id": "cv-1", "canvas_ledger": ledger})
    tc = {"id": "call_1", "name": "add_text_block", "arguments": "{}"}
    with patch("app.services.agents.tool_executor.ToolRegistry.get", return_value=FakeLedgerAwareTool()):
        r = await exc.execute_tool_call(tc)
    assert not r.is_error
    assert FakeLedgerAwareTool.seen["ledger"] is ledger, "必须是同一个台账实例（否则跨轮次失忆）"
    assert FakeLedgerAwareTool.seen["canvas_id"] == "cv-1"