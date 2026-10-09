"""阶段 4 兜底单测：

1. `agent_orchestrator._agentic_run_step` 的 2a 逃逸分支 —— 无工具调用不再"文本即完成"。
2. `api.v1.ai._lead_stream_guard` —— 主导 Agent 异常时回落旧路径 / 降级收尾。
"""
import json
from unittest.mock import MagicMock

from app.services.agent_tools import ToolRegistry
from app.services.agents.agent_orchestrator import AgentOrchestrator, _has_tool_action
from app.api.v1.ai import _lead_stream_guard


# ── 1. agent_orchestrator 2a goal 达成校验 ──


def test_has_tool_action_judge():
    """达成判据：真实工具结果算达成；纯文本占位 / skipped / error / 非 JSON 均未达成。"""
    assert _has_tool_action(json.dumps({"columns": ["c"], "rows": [[1]]})) is True
    assert _has_tool_action(json.dumps({"text": "我看了下数据"})) is False
    assert _has_tool_action(json.dumps({"skipped": True})) is False
    assert _has_tool_action(json.dumps({"error": "boom"})) is False
    assert _has_tool_action("不是 JSON") is False


class MockLLM:
    """流式 mock：按 push 顺序返回 tool_call / text。"""

    def __init__(self):
        self._responses: list[dict] = []

    def push_tool_call(self, name: str, args: dict) -> None:
        self._responses.append({
            "type": "tool_call",
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
            "id": f"call_{len(self._responses)}",
        })

    def push_text(self, content: str) -> None:
        self._responses.append({"type": "text", "content": content})

    async def stream_chat_with_tools(self, messages, tools, **kw):
        if self._responses:
            yield self._responses.pop(0)
        else:
            yield {"type": "text", "content": "完成"}


class _FakeTool:
    """模拟工具：execute 返回固定结果。"""

    def __init__(self, name: str, fn):
        self.name = name
        self._fn = fn

    async def execute(self, **kwargs):
        return self._fn(**kwargs)


def install_tool_registry(monkeypatch, tools: dict):
    def fake_get(name):
        return tools.get(name)

    def fake_schemas():
        return [
            {"type": "function", "function": {"name": n, "description": f"mock {n}", "parameters": {"type": "object", "properties": {}}}}
            for n in tools
        ]

    monkeypatch.setattr(ToolRegistry, "get", staticmethod(fake_get))
    monkeypatch.setattr(ToolRegistry, "schemas", staticmethod(fake_schemas))


def _make_orch(extra: set[str] | None = None):
    """用 __new__ 构造（绕过 planner/LLM 依赖），注入可规划工具白名单。"""
    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch.llm = None
    orch.db_session = None
    orch.extra_plannable_tools = set(extra) if extra is not None else {"query_engine"}
    orch.planner = MagicMock()
    orch.graph = orch._build_graph()
    return orch


def _make_step(sid: int, tool: str, depends_on: list | None = None, goal: str | None = None) -> dict:
    return {
        "step_id": sid,
        "goal": goal or f"步骤 {sid}",
        "tool": tool,
        "depends_on": depends_on or [],
        "purpose": "p",
    }


def _shared(emit, **over):
    base = {
        "emit": emit, "db_session": None, "user_msg": "查一下", "history": [],
        "available_datasources": [], "state_sink": {}, "tool_memo": {}, "tool_memo_locks": {},
    }
    base.update(over)
    return base


async def test_no_tool_call_text_forced_retry(monkeypatch):
    """LLM 第一轮只输出文本（无工具调用）→ 不收尾，回灌强制调用工具 → 第二轮成功。"""
    install_tool_registry(monkeypatch, {
        "query_engine": _FakeTool("query_engine", lambda **kw: json.dumps(
            {"columns": ["c"], "rows": [[1]]}, ensure_ascii=False)),
    })
    mock_llm = MockLLM()
    mock_llm.push_text("我先说说思路，暂不查数")           # 第一轮逃逸
    mock_llm.push_tool_call("query_engine", {"sql": "select 1"})  # 第二轮正确调用
    orch = _make_orch(extra={"query_engine"})
    orch.llm = mock_llm

    results: dict = {}
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    await orch._agentic_run_step(
        _make_step(1, "query_engine"), results, {"user_id": "u1"}, **_shared(emit)
    )

    parsed = json.loads(results[1])
    assert parsed.get("rows") == [[1]], results[1]
    # 纯文本逃逸没有被当成步骤结果
    assert not results[1].startswith('{"text"')


async def test_no_tool_call_twice_skipped(monkeypatch):
    """连续两轮无工具调用 → 判定 goal 未达成，显式跳过（不当成功）。"""
    install_tool_registry(monkeypatch, {
        "query_engine": _FakeTool("query_engine", lambda **kw: json.dumps(
            {"columns": ["c"], "rows": [[1]]}, ensure_ascii=False)),
    })
    mock_llm = MockLLM()
    mock_llm.push_text("第一轮：不好弄")
    mock_llm.push_text("第二轮：还是不弄")  # 仍无工具调用 → streak 达到阈值
    orch = _make_orch(extra={"query_engine"})
    orch.llm = mock_llm

    results: dict = {}

    async def emit(ev):
        pass

    await orch._agentic_run_step(
        _make_step(1, "query_engine"), results, {"user_id": "u1"}, **_shared(emit)
    )

    parsed = json.loads(results[1])
    assert parsed.get("skipped") is True, results[1]
    assert "goal 未达成" in parsed.get("skipped_reason", "")


# ── 2. api 层主导 Agent 降级护栏 ──


async def test_guard_falls_back_when_lead_raises_before_events():
    """主导 Agent 未产出任何事件即异常 → 整体回落旧路径，done 标记 degraded。"""

    async def lead_stream():
        raise RuntimeError("intent boom")
        yield  # pragma: no cover

    legacy_calls = {"n": 0}

    async def legacy_factory():
        legacy_calls["n"] += 1
        yield {"type": "text", "content": "旧路径回答"}
        yield {"type": "done", "degraded": False}

    events = [ev async for ev in _lead_stream_guard(lead_stream(), legacy_factory)]
    types = [e["type"] for e in events]
    assert types == ["status", "text", "done"], types
    assert events[0]["degradation"] == "lead_agent_fallback"
    assert events[-1]["degraded"] is True
    assert legacy_calls["n"] == 1


async def test_guard_degrades_after_partial_events():
    """已产出部分事件后异常 → 不重跑旧路径，补发 status + done(degraded=True)。"""

    async def lead_stream():
        yield {"type": "intent", "intent": "data_qa"}
        raise RuntimeError("analysis boom")

    async def legacy_factory():
        raise AssertionError("已产出事件后不应回落旧路径")
        yield  # pragma: no cover

    events = [ev async for ev in _lead_stream_guard(lead_stream(), legacy_factory)]
    types = [e["type"] for e in events]
    assert types == ["intent", "status", "done"], types
    assert events[-1]["degraded"] is True


async def test_guard_passthrough_when_no_error():
    """正常情况：原样透传主导 Agent 事件，不触发旧路径。"""

    async def lead_stream():
        yield {"type": "intent", "intent": "chat"}
        yield {"type": "done", "degraded": False}

    async def legacy_factory():
        raise AssertionError("不应回落旧路径")
        yield  # pragma: no cover

    events = [ev async for ev in _lead_stream_guard(lead_stream(), legacy_factory)]
    assert [e["type"] for e in events] == ["intent", "done"]
    assert events[-1]["degraded"] is False
