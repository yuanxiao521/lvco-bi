"""CanvasOrchestrator 单测：报告骨架规划 → 落块执行 → 总结，严格白名单。

覆盖核心差异化：
- 图结构（plan → execute_steps → finish）
- Executor 严格白名单（只执行入口注入的画布工具）
- 落块执行（add_text_block / add_chart_block 经 ToolExecutor 执行）
- Planner 失败降级计划
"""
import json
from unittest.mock import MagicMock

from app.services.agent_tools import ToolRegistry
from app.services.agents.canvas_orchestrator import CanvasOrchestrator, _has_canvas_action


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

    async def complete(self, messages, **kw):
        return "{}"


def _make_orch(extra: set[str] | None = None):
    """用 __new__ 构造（绕过 yaml/LLM 依赖），注入画布工具白名单。"""
    orch = CanvasOrchestrator.__new__(CanvasOrchestrator)
    orch.llm = None
    orch.db_session = None
    # 注意：不能用 `extra or default`（空 set 是 falsy），须显式判断 None
    orch.extra_plannable_tools = set(extra) if extra is not None else {"add_text_block", "add_chart_block"}
    orch.planner = MagicMock()
    orch._executor_system = "你是画布执行 Agent，只执行当前落块步骤。"
    orch.graph = orch._build_graph()
    return orch


def install_tool_registry(monkeypatch, tools: dict):
    """monkeypatch ToolRegistry.get/schemas，注册 mock 画布工具。"""

    def fake_get(name):
        return tools.get(name)

    def fake_schemas():
        return [
            {"type": "function", "function": {"name": n, "description": f"mock {n}", "parameters": {"type": "object", "properties": {}}}}
            for n in tools
        ]

    monkeypatch.setattr(ToolRegistry, "get", staticmethod(fake_get))
    monkeypatch.setattr(ToolRegistry, "schemas", staticmethod(fake_schemas))


def _make_step(sid: int, tool: str, depends_on: list | None = None, goal: str | None = None) -> dict:
    return {
        "step_id": sid,
        "goal": goal or f"步骤 {sid}",
        "tool": tool,
        "depends_on": depends_on or [],
        "purpose": "p",
    }


# ── 图结构 ──

def test_graph_built_correctly():
    orch = _make_orch()
    assert set(orch.graph._nodes) == {"plan", "execute_steps", "finish"}
    assert orch.graph._entry == "plan"
    assert "finish" in orch.graph._finish
    # plan 条件边：ok→execute_steps, done→finish
    _, mapping = orch.graph._conditional["plan"]
    assert mapping == {"ok": "execute_steps", "done": "finish"}
    # execute_steps 顺序边 → finish
    assert orch.graph._edges["execute_steps"] == "finish"


# ── 严格白名单 ──

def test_executor_tools_only_whitelist(monkeypatch):
    install_tool_registry(monkeypatch, {
        "add_text_block": object(),
        "add_chart_block": object(),
        "render_chart": object(),  # 非画布工具，不应出现在 Executor 工具列表
        "query_sql": object(),
    })
    orch = _make_orch(extra={"add_text_block", "add_chart_block"})
    names = [t["function"]["name"] for t in orch._executor_tools()]
    assert set(names) == {"add_text_block", "add_chart_block"}
    assert "render_chart" not in names
    assert "query_sql" not in names


def test_executor_tools_empty_without_whitelist(monkeypatch):
    install_tool_registry(monkeypatch, {"add_text_block": object()})
    orch = _make_orch(extra=set())
    assert orch._executor_tools() == []


# ── 落块执行（mini-ReAct） ──

async def test_execute_add_text_block(monkeypatch):
    """执行 add_text_block 落块：LLM 调工具 → ToolExecutor 执行 → 步骤结果含 canvas_action。"""
    install_tool_registry(monkeypatch, {
        "add_text_block": _FakeTool("add_text_block", lambda **kw: json.dumps({
            "ok": True,
            "canvas_action": {"action": "add_text_block", "block": {"blockType": kw.get("block_type"), "content": kw.get("content")}},
        }, ensure_ascii=False)),
    })
    mock_llm = MockLLM()
    mock_llm.push_tool_call("add_text_block", {"block_type": "h1", "content": "销售分析报告"})
    orch = _make_orch()
    orch.llm = mock_llm

    results: dict = {}
    events: list[dict] = []

    async def emit(ev):
        events.append(ev)

    shared = {
        "emit": emit,
        "db_session": None,
        "user_msg": "做一份销售报告",
        "history": [],
        "available_datasources": [],
        "state_sink": {},
        "tool_memo": {},
        "tool_memo_locks": {},
    }
    state = {"user_id": "u1"}

    await orch._agentic_run_step(_make_step(1, "add_text_block"), results, state, **shared)

    parsed = json.loads(results[1])
    assert parsed["ok"] is True
    assert parsed["canvas_action"]["action"] == "add_text_block"
    # 落块动作应被 emit（tool_result 事件由 ToolExecutor 发出）
    assert any(e.get("type") == "tool_result" for e in events)


async def test_execute_steps_node_with_dependency(monkeypatch):
    """依赖步骤先执行（图表→叙事），同层并发。"""
    install_tool_registry(monkeypatch, {
        "add_chart_block": _FakeTool("add_chart_block", lambda **kw: json.dumps({
            "ok": True,
            "canvas_action": {"action": "add_chart_block", "block": {"title": kw.get("title")}},
        }, ensure_ascii=False)),
        "add_text_block": _FakeTool("add_text_block", lambda **kw: json.dumps({
            "ok": True,
            "canvas_action": {"action": "add_text_block", "block": {"content": kw.get("content")}},
        }, ensure_ascii=False)),
    })
    mock_llm = MockLLM()
    mock_llm.push_tool_call("add_chart_block", {"title": "趋势图", "chart_type": "line", "datasource_id": "ds1", "dimensions": ["month"], "measures": [{"field": "sales", "agg": "SUM"}]})
    mock_llm.push_tool_call("add_text_block", {"block_type": "text", "content": "销售额呈上升趋势"})
    orch = _make_orch()
    orch.llm = mock_llm

    events: list[dict] = []
    async def emit(ev):
        events.append(ev)

    shared = {
        "emit": emit, "db_session": None, "user_msg": "报告", "history": [],
        "available_datasources": [{"id": "ds1", "name": "销售", "fields": [{"name": "month", "data_type": "VARCHAR"}, {"name": "sales", "data_type": "BIGINT"}]}],
        "state_sink": {}, "tool_memo": {}, "tool_memo_locks": {},
    }
    state = {
        "user_id": "u1",
        "ordered_steps": [
            _make_step(1, "add_chart_block"),
            _make_step(2, "add_text_block", depends_on=[1]),
        ],
    }
    await orch._execute_steps_node(state, **shared)
    results = shared["state_sink"]["results"]
    assert 1 in results and 2 in results
    assert json.loads(results[1])["ok"] is True
    assert json.loads(results[2])["ok"] is True


# ── Planner 失败降级 ──

def test_fallback_plan_structure():
    plan = CanvasOrchestrator._build_fallback_plan("用户问销售额", [{"id": "ds1", "name": "d"}])
    steps = plan["steps"]
    assert steps[0]["tool"] == "add_text_block"
    assert steps[1]["tool"] == "add_chart_block"
    assert steps[2]["tool"] == "add_text_block"
    assert steps[2]["depends_on"] == [2]


# ── goal 达成校验（防"无工具调用直接收尾"逃逸） ──


def test_has_canvas_action_judge():
    """达成判据：带 canvas_action 才算完成；纯文本/error/非 JSON 均未达成。"""
    assert _has_canvas_action('{"ok": true, "canvas_action": {"action": "add_chart_block", "block": {}}}') is True
    assert _has_canvas_action('{"error": "维度为空"}') is False
    assert _has_canvas_action('{"text": "我看了下数据"}') is False
    assert _has_canvas_action("不是 JSON") is False


async def test_no_tool_call_text_output_forced_retry(monkeypatch):
    """LLM 第一轮只输出文本（无工具调用）→ 不收尾，回灌强制落块 → 第二轮成功。

    旧逻辑把纯文本当结果直接结束（2a 逃逸），导致建图步骤不建图也被判成功；
    新逻辑必须产出 canvas_action 才算完成。
    """
    install_tool_registry(monkeypatch, {
        "add_text_block": _FakeTool("add_text_block", lambda **kw: json.dumps({
            "ok": True,
            "canvas_action": {"action": "add_text_block", "block": {"blockType": kw.get("block_type"), "content": kw.get("content")}},
        }, ensure_ascii=False)),
    })
    mock_llm = MockLLM()
    # 第一轮：偷懒只输出文本；第二轮：正确落块
    mock_llm.push_text("先总结一下，暂不落块")
    mock_llm.push_tool_call("add_text_block", {"block_type": "h2", "content": "章节标题"})
    orch = _make_orch()
    orch.llm = mock_llm

    results: dict = {}
    async def emit(ev):
        pass

    shared = {
        "emit": emit, "db_session": None, "user_msg": "报告", "history": [],
        "available_datasources": [], "state_sink": {}, "tool_memo": {}, "tool_memo_locks": {},
    }
    state = {"user_id": "u1"}

    await orch._agentic_run_step(_make_step(1, "add_text_block"), results, state, **shared)

    parsed = json.loads(results[1])
    assert parsed["ok"] is True, results[1]
    assert parsed["canvas_action"]["action"] == "add_text_block"
    # 落块成功前没有把"纯文本"当结果
    assert not results[1].startswith('{"text"')


async def test_no_tool_call_twice_skipped(monkeypatch):
    """连续两轮无工具调用 → 判定 goal 未达成，显式跳过（不当成功）。"""
    install_tool_registry(monkeypatch, {
        "add_chart_block": _FakeTool("add_chart_block", lambda **kw: json.dumps({
            "ok": True,
            "canvas_action": {"action": "add_chart_block", "block": {"title": kw.get("title")}},
        }, ensure_ascii=False)),
    })
    mock_llm = MockLLM()
    mock_llm.push_text("第一轮：不好弄")
    mock_llm.push_text("第二轮：还是不弄")  # 第二轮仍无工具调用 → streak 达到阈值
    orch = _make_orch()
    orch.llm = mock_llm

    results: dict = {}
    async def emit(ev):
        pass

    shared = {
        "emit": emit, "db_session": None, "user_msg": "报告", "history": [],
        "available_datasources": [], "state_sink": {}, "tool_memo": {}, "tool_memo_locks": {},
    }
    state = {"user_id": "u1"}

    await orch._agentic_run_step(_make_step(1, "add_chart_block"), results, state, **shared)

    parsed = json.loads(results[1])
    assert parsed.get("skipped") is True, results[1]
    assert "goal 未达成" in parsed.get("skipped_reason", "")


# ── 收尾节点 ──

async def test_finish_node_emits_done():
    orch = _make_orch()
    events: list[dict] = []
    async def emit(ev):
        events.append(ev)
    shared = {"emit": emit}
    await orch._finish_node({"results": {1: '{"ok": true, "canvas_action": {}}'}}, **shared)
    types = [e["type"] for e in events]
    assert "status" in types and "text" in types and "done" in types


class _FakeTool:
    """模拟画布工具：execute 返回固定结果。"""

    def __init__(self, name: str, fn):
        self.name = name
        self._fn = fn

    async def execute(self, **kwargs):
        return self._fn(**kwargs)
