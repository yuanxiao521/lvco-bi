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