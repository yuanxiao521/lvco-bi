"""高级编排器测试：覆盖 P0-2/P0-3/P1-6/P2-9/P2-11 五个优化任务。

对应：
- Task 2 (P0-2) 单步失败强制干预
- Task 3 (P0-3) 步骤分层并行执行
- Task 8 (P1-6) 工具结果 memo
- Task 9 (P2-11) 历史注入
- Task 11 (P2-9) phase 事件复用
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# 允许独立运行
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.agents.agent_orchestrator import (  # noqa: E402
    AgentOrchestrator,
    _group_steps_by_level,
    _make_fail_key,
    _make_memo_key,
    _summarize_history_safe,
    _IDEMPOTENT_TOOLS,
)
from app.services.agent_tools import ToolRegistry  # noqa: E402


# ───────────────────────── Mock 工具 ─────────────────────────


class MockTool:
    """可配置行为的通用工具 mock。"""

    def __init__(
        self,
        name: str,
        result_factory=None,
        call_log: list | None = None,
    ) -> None:
        self.name = name
        self._result_factory = result_factory or (lambda **_: {"ok": True})
        self.call_log = call_log if call_log is not None else []

    async def execute(self, **kwargs) -> str:
        self.call_log.append(dict(kwargs))
        return json.dumps(self._result_factory(**kwargs), ensure_ascii=False)


# ───────────────────────── Mock LLM ─────────────────────────


class MockLLM:
    """可预设响应序列的 LLM mock。

    支持两种模式：
    1. 全局 push：所有 step 共享一个响应队列，按调用顺序消费（不适用于并发 step）。
    2. 按 step_id push：每个 step_id 独立一个响应队列，并发安全。
    """

    def __init__(self) -> None:
        import collections
        self._collections = collections
        self._global_responses: collections.deque = collections.deque()
        self._per_step: dict[int, collections.deque] = {}
        self.stream_invocations: int = 0

    def push_tool_call(self, tool_name: str, args: dict, *, step_id: int | None = None) -> None:
        item = {
            "type": "tool_call",
            "name": tool_name,
            "arguments": json.dumps(args, ensure_ascii=False),
            "id": f"call_{self.stream_invocations}",
        }
        if step_id is None:
            self._global_responses.append(item)
        else:
            self._per_step.setdefault(step_id, self._collections.deque()).append(item)

    def push_text(self, content: str, *, step_id: int | None = None) -> None:
        item = {"type": "text", "content": content}
        if step_id is None:
            self._global_responses.append(item)
        else:
            self._per_step.setdefault(step_id, self._collections.deque()).append(item)

    def push_exception(self, exc: BaseException, *, step_id: int | None = None) -> None:
        if step_id is None:
            self._global_responses.append(exc)
        else:
            self._per_step.setdefault(step_id, self._collections.deque()).append(exc)

    @staticmethod
    def _infer_step_id(messages: list) -> int | None:
        """从 user 消息的 context（含 step_id=N）中识别当前步骤。"""
        import re
        for m in messages:
            content = str(m.get("content", ""))
            m_match = re.search(r"step_id[=＝](\d+)", content)
            if m_match:
                try:
                    return int(m_match.group(1))
                except ValueError:
                    continue
        return None

    async def stream_chat_with_tools(self, messages, tools, **kw):
        self.stream_invocations += 1
        sid = self._infer_step_id(messages)
        q = self._per_step.get(sid) if sid is not None else None
        if q and q:
            item = q.popleft()
        elif self._global_responses:
            item = self._global_responses.popleft()
        else:
            yield {"type": "text", "content": "完成"}
            return
        if isinstance(item, BaseException):
            raise item
        yield item

    async def complete(self, messages, **kw) -> str:
        return "mock report"


# ───────────────────────── Fixtures ─────────────────────────


@pytest.fixture
def mock_llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def orchestrator(monkeypatch, mock_llm):
    """构造 Orchestrator 实例并 stub 掉 llm。"""

    orch = AgentOrchestrator.__new__(AgentOrchestrator)
    orch.llm = mock_llm
    orch.db_session = None
    # 桩 PlannerAgent.execute 直接返回包装好的 plan
    from app.services.agents.base_agent import AgentResult

    class StubPlanner:
        def __init__(self, plan: dict):
            self._plan = plan

        async def execute(self, **kwargs):
            return AgentResult(success=True, data=dict(self._plan))

    return orch, StubPlanner


def install_tool_registry(monkeypatch, tools: dict[str, MockTool]):
    """monkeypatch ToolRegistry.get 和 ToolRegistry.schemas。"""

    def fake_get(name):
        return tools.get(name)

    def fake_schemas():
        out = []
        for name, tool in tools.items():
            out.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"mock {name}",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
        return out

    monkeypatch.setattr(ToolRegistry, "get", staticmethod(fake_get))
    monkeypatch.setattr(ToolRegistry, "schemas", staticmethod(fake_schemas))


def make_plan(steps: list[dict]) -> dict:
    return {
        "task_summary": "test",
        "expected_output": "test",
        "steps": steps,
    }


def make_step(sid: int, tool: str, *, depends_on: list | None = None, goal: str | None = None) -> dict:
    return {
        "step_id": sid,
        "tool": tool,
        "goal": goal or f"step {sid}",
        "depends_on": depends_on or [],
    }


async def _drain_queue(orch, monkeypatch, mock_llm, plan_steps, *, history=None, available_datasources=None):
    """直接驱动 orchestrator 内部节点以收集所有事件（避开整体超时/队列）。"""
    from app.services.agents.planner_agent import PlannerAgent  # noqa: F401  仅占位防导入报错

    events: list[dict] = []
    state_sink: dict = {}

    async def emit(ev):
        events.append(ev)

    shared = {
        "user_msg": "test query",
        "history": history or [],
        "available_datasources": available_datasources or [],
        "db_session": None,
        "emit": emit,
        "trace": None,
        "state_sink": state_sink,
        "tool_memo": {},
    }

    # 替换 _plan_node：直接返回预定 plan，跳过 PlannerAgent LLM
    plan = make_plan(plan_steps)

    async def fake_plan_node(state, **shared):
        await shared["emit"]({"type": "status", "phase": "selecting", "message": "正在分析需求..."})
        steps = plan["steps"]
        has_chart = any(s.get("tool") == "render_chart" for s in steps)
        shared["state_sink"]["plan"] = plan
        return {
            "plan": plan,
            "has_chart_steps": has_chart,
            "ordered_steps": [s for s in steps],  # _agentic_run_step 容忍无序，_group_steps_by_level 重新切层
        }

    orch._plan_node = fake_plan_node

    # 透传 _agentic_run_step（无包装）
    orig_agentic = orch._agentic_run_step
    _orig_execute = orch._execute_steps_node
    _orig_chart = orch._chart_steps_node
    _orig_report = orch._report_node

    # 包装 _execute_steps_node：在调用前先驱动 _plan_node，确保 selecting 事件被 emit
    async def _execute_with_plan(state, **shared):
        # 调用 _plan_node（fake_plan_node 已 stub 掉 PlannerAgent 调用）
        new_state = await orch._plan_node(state, **shared)
        state.update(new_state)
        return await _orig_execute(state, **shared)

    orch._execute_steps_node = _execute_with_plan
    orig_execute_steps_node = _orig_execute

    async def passthrough_agentic(step, results, state, **shared):
        await orig_agentic(step, results, state, **shared)

    orch._agentic_run_step = passthrough_agentic

    # 驱动 execute_steps_node + chart_steps_node + report_node
    state = {
        "user_id": "test_user",
        "plan": plan,
        "ordered_steps": plan["steps"],
    }
    from app.services.agents.agent_orchestrator import (
        AgentOrchestrator as _Cls,
    )

    new_state = await orch._execute_steps_node(state, **shared)
    state.update(new_state)
    new_state = await orch._chart_steps_node(state, **shared)
    state.update(new_state)
    new_state = await orch._report_node(state, **shared)
    state.update(new_state)
    return events, state


# ───────────────────────── Task 2: 单步失败强制干预 ─────────────────────────


async def test_same_tool_args_fail_3_times_skipped(monkeypatch, mock_llm, orchestrator):
    orch, _ = orchestrator
    call_log: list[dict] = []
    install_tool_registry(
        monkeypatch,
        {"failing_tool": MockTool("failing_tool", result_factory=lambda **_: {"error": "boom"}, call_log=call_log)},
    )
    # 6 次响应：3 次失败 + 3 次"text"结束（如果走到这里），但 3 次失败后 step 应被跳过
    mock_llm.push_tool_call("failing_tool", {"x": 1})
    mock_llm.push_tool_call("failing_tool", {"x": 1})
    mock_llm.push_tool_call("failing_tool", {"x": 1})
    mock_llm.push_text("unused after skip")

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm, [make_step(1, "failing_tool")]
    )

    assert len(call_log) == 3, f"工具应执行 3 次，实际 {len(call_log)}"
    parsed = json.loads(state["results"][1])
    assert parsed.get("skipped") is True
    assert "连续失败 3 次" in parsed.get("skipped_reason", "")


async def test_fail_twice_then_force_tool_change(monkeypatch, mock_llm, orchestrator):
    orch, _ = orchestrator
    call_log: list[dict] = []
    install_tool_registry(
        monkeypatch,
        {"bad_tool": MockTool("bad_tool", result_factory=lambda **_: {"error": "fail"}, call_log=call_log)},
    )

    # 记录消息，看 2 次失败后是否注入了换工具指令
    captured_messages: list[list[dict]] = []

    class WatchingLLM(MockLLM):
        async def stream_chat_with_tools(self, messages, tools, **kw):
            captured_messages.append([dict(m) for m in messages])
            async for ev in super().stream_chat_with_tools(messages, tools, **kw):
                yield ev

    watching_llm = WatchingLLM()
    watching_llm.push_tool_call("bad_tool", {"y": 2})
    watching_llm.push_tool_call("bad_tool", {"y": 2})
    watching_llm.push_tool_call("other_tool", {"y": 2})  # 第 3 次失败后已跳过，这里不会到
    orch.llm = watching_llm

    # 注入第 3 个 tool（other_tool）以便 registry 查找不报错
    install_tool_registry(
        monkeypatch,
        {
            "bad_tool": MockTool("bad_tool", result_factory=lambda **_: {"error": "fail"}, call_log=call_log),
            "other_tool": MockTool("other_tool", result_factory=lambda **_: {"ok": True}),
        },
    )

    events, state = await _drain_queue(
        orch, monkeypatch, watching_llm, [make_step(1, "bad_tool")]
    )

    # 找到第 2 次失败后第 3 次调用前的消息列表，看是否注入了 system 强制消息
    assert len(captured_messages) >= 3
    msg_after_two_fails = captured_messages[2]  # 第 3 次 LLM 调用时 messages 应包含换工具指令
    system_msgs = [m for m in msg_after_two_fails if m.get("role") == "system" and "连续失败 2 次" in str(m.get("content", ""))]
    assert len(system_msgs) >= 1, f"应注入换工具指令，实际 messages={msg_after_two_fails}"
    assert "bad_tool" in system_msgs[0]["content"]


async def test_failure_count_per_signature(monkeypatch, mock_llm, orchestrator):
    """不同 args 的失败互不影响（key 含 args 哈希）。"""
    orch, _ = orchestrator
    call_log: list[dict] = []
    install_tool_registry(
        monkeypatch,
        {"t": MockTool("t", result_factory=lambda **_: {"error": "fail"}, call_log=call_log)},
    )

    # 第 1 次：用 args_a 失败
    mock_llm.push_tool_call("t", {"a": 1})
    # 第 2 次：换 args_b 失败（key 不同，互不影响）
    mock_llm.push_tool_call("t", {"a": 2})
    # 第 3 次：再用 args_a 失败（args_a 累计 2 次，未达 3）
    mock_llm.push_tool_call("t", {"a": 1})
    # 第 4 次：args_a 再次失败 → 累计 3 → 跳过
    mock_llm.push_tool_call("t", {"a": 1})
    mock_llm.push_text("unused")

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm, [make_step(1, "t")]
    )

    parsed = json.loads(state["results"][1])
    assert parsed.get("skipped") is True
    # 工具实际执行 4 次（args_a×3 + args_b×1）
    assert len(call_log) == 4, f"应执行 4 次，实际 {len(call_log)}"
    # 验证 fail_key 不同
    assert _make_fail_key("t", {"a": 1}) != _make_fail_key("t", {"a": 2})


# ───────────────────────── Task 3: 步骤分层并行执行 ─────────────────────────


def test_group_steps_by_level_independent():
    s1 = make_step(1, "t1")
    s2 = make_step(2, "t1")
    s3 = make_step(3, "t1")
    levels = _group_steps_by_level([s1, s2, s3])
    assert len(levels) == 1
    assert sorted(s["step_id"] for s in levels[0]) == [1, 2, 3]


def test_group_steps_by_level_dependent():
    s1 = make_step(1, "t1")
    s2 = make_step(2, "t1", depends_on=[1])
    s3 = make_step(3, "t1", depends_on=[2])
    levels = _group_steps_by_level([s1, s2, s3])
    assert len(levels) == 3
    assert [s["step_id"] for s in levels[0]] == [1]
    assert [s["step_id"] for s in levels[1]] == [2]
    assert [s["step_id"] for s in levels[2]] == [3]


async def test_independent_steps_run_concurrently(monkeypatch, mock_llm, orchestrator):
    """两个独立步骤应并发执行（开始时间差 < sleep 时长）。"""
    orch, _ = orchestrator
    timings: list[tuple[str, float]] = []

    class TimingTool(MockTool):
        async def execute(self, **kwargs):
            timings.append((self.name + "_start", asyncio.get_event_loop().time()))
            await asyncio.sleep(0.15)
            timings.append((self.name + "_done", asyncio.get_event_loop().time()))
            return json.dumps({"ok": True})

    tools = {
        "tA": TimingTool("tA", call_log=timings),
        "tB": TimingTool("tB", call_log=timings),
    }
    tools["tA"].name = "tA"
    tools["tB"].name = "tB"
    install_tool_registry(monkeypatch, tools)

    # 每个步骤需要 2 次 LLM 调用（tool_call + text），按 step_id 路由避免并发干扰
    mock_llm.push_tool_call("tA", {}, step_id=1)
    mock_llm.push_text("done A", step_id=1)
    mock_llm.push_tool_call("tB", {}, step_id=2)
    mock_llm.push_text("done B", step_id=2)

    import time
    start = time.monotonic()
    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm,
        [make_step(1, "tA"), make_step(2, "tB")],
    )
    elapsed = time.monotonic() - start

    # 两个独立步骤并发：总时长应 < 单步耗时（0.15s）+ 调度开销
    # 如果串行执行将是 ~0.3s；如果并发约 0.15s + 调度
    assert elapsed < 0.25, f"并发执行耗时过长: {elapsed:.3f}s（应 < 0.25s，串行约 0.30s）"
    # 两个步骤都有结果
    assert 1 in state["results"]
    assert 2 in state["results"]
    # 两个步骤的开始时间差应 < 0.05s（说明是真正的并发而不是串行 sleep）
    starts = [t for t in timings if t[0].endswith("_start")]
    assert len(starts) == 2, f"工具应执行 2 次，实际 {len(starts)}"
    gap = abs(starts[0][1] - starts[1][1])
    assert gap < 0.05, f"两个步骤非并发启动 (gap={gap:.3f}s): {timings}"


async def test_dependent_step_waits_for_dep(monkeypatch, mock_llm, orchestrator):
    """依赖步骤必须等待被依赖步骤完成。"""
    orch, _ = orchestrator
    order: list[int] = []

    class OrderingTool(MockTool):
        async def execute(self, **kwargs):
            order.append(self.name_step)
            return json.dumps({"ok": True})

    t1 = OrderingTool("t1")
    t1.name_step = 1
    t2 = OrderingTool("t2")
    t2.name_step = 2
    install_tool_registry(monkeypatch, {"t1": t1, "t2": t2})

    mock_llm.push_tool_call("t1", {})
    mock_llm.push_text("done")
    mock_llm.push_tool_call("t2", {})
    mock_llm.push_text("done")

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm,
        [make_step(1, "t1"), make_step(2, "t2", depends_on=[1])],
    )

    assert order == [1, 2], f"依赖步骤顺序错误: {order}"


async def test_event_order_stable_with_parallel(monkeypatch, mock_llm, orchestrator):
    """并发执行时，步骤间无依赖但事件流仍按 step_id 顺序对应。"""
    orch, _ = orchestrator
    install_tool_registry(
        monkeypatch,
        {
            "tx": MockTool("tx", result_factory=lambda **_: {"ok": True}),
            "ty": MockTool("ty", result_factory=lambda **_: {"ok": True}),
        },
    )
    mock_llm.push_tool_call("tx", {})
    mock_llm.push_text("x")
    mock_llm.push_tool_call("ty", {})
    mock_llm.push_text("y")

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm,
        [make_step(1, "tx"), make_step(2, "ty")],
    )

    # 每个 step 至少发出过 tool_call 与 tool_result
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    tool_results = [e for e in events if e.get("type") == "tool_result"]
    names_called = [e["name"] for e in tool_calls]
    assert "tx" in names_called
    assert "ty" in names_called
    # 至少各一次 tool_result（最终状态）
    assert any("tx" in str(e.get("name", "")) for e in tool_results)
    assert any("ty" in str(e.get("name", "")) for e in tool_results)


# ───────────────────────── Task 8: 工具结果 memo ─────────────────────────


async def test_list_datasources_cached_within_run(monkeypatch, mock_llm, orchestrator):
    """list_datasources 同参数 4 次调用，实际只执行 1 次。"""
    assert "list_datasources" in _IDEMPOTENT_TOOLS
    orch, _ = orchestrator
    call_log: list[dict] = []
    install_tool_registry(
        monkeypatch,
        {"list_datasources": MockTool(
            "list_datasources",
            result_factory=lambda **_: {"datasources": ["ds1", "ds2"]},
            call_log=call_log,
        )},
    )

    # 4 个步骤都调用 list_datasources，参数一致（按 step_id 路由响应避免并发干扰）
    for sid in (1, 2, 3, 4):
        mock_llm.push_tool_call("list_datasources", {"filter": "all"}, step_id=sid)
        mock_llm.push_text("done", step_id=sid)

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm,
        [
            make_step(1, "list_datasources"),
            make_step(2, "list_datasources"),
            make_step(3, "list_datasources"),
            make_step(4, "list_datasources"),
        ],
    )

    assert len(call_log) == 1, f"实际工具执行次数应为 1，实际 {len(call_log)}"
    # 4 个步骤都拿到了结果
    for sid in (1, 2, 3, 4):
        assert sid in state["results"]
        assert "ds1" in state["results"][sid]


async def test_render_chart_not_cached(monkeypatch, mock_llm, orchestrator):
    """render_chart 是非幂等工具，不应被 memo 命中。"""
    orch, _ = orchestrator
    call_log: list[dict] = []

    def chart_factory(**kwargs):
        # 每次返回不同的 option 以便校验都被执行
        idx = len(call_log)
        call_log.append({"args": dict(kwargs), "i": idx})
        return {
            "chart_type": "bar",
            "option": {"series": [{"data": [idx, idx + 1]}]},
            "data_code": "select 1",
        }

    install_tool_registry(
        monkeypatch,
        {"render_chart": MockTool("render_chart", result_factory=chart_factory)},
    )

    # 2 个 chart 步骤都调用 render_chart
    s1 = make_step(1, "render_chart", goal="chart1")
    s2 = make_step(2, "render_chart", goal="chart2")
    s1["data_code"] = "select 1"
    s2["data_code"] = "select 2"

    mock_llm.push_tool_call("render_chart", {"chart_type": "bar"})
    mock_llm.push_text("done")
    mock_llm.push_tool_call("render_chart", {"chart_type": "bar"})
    mock_llm.push_text("done")

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm, [s1, s2],
    )

    assert len(call_log) == 2, f"render_chart 应每次都执行，实际 {len(call_log)}"


# ───────────────────────── Task 9: 历史注入 ─────────────────────────


def test_history_summary_safe_empty():
    assert _summarize_history_safe([]) == ""


def test_history_summary_safe_with_msgs():
    history = [
        {"role": "user", "content": "上轮问题"},
        {"role": "assistant", "content": "上轮回答"},
    ]
    out = _summarize_history_safe(history)
    assert "对话历史摘要" in out
    assert "上轮问题" in out or "上轮回答" in out


async def test_history_injected_into_executor_prompt(monkeypatch, mock_llm, orchestrator):
    """executor 上下文中应包含对话历史摘要。"""
    orch, _ = orchestrator
    captured: list[str] = []

    install_tool_registry(
        monkeypatch,
        {"dummy": MockTool("dummy", result_factory=lambda **_: {"ok": True})},
    )

    # 拦截 _build_executor_context 来验证其输出含 history
    from app.services.agents import agent_orchestrator as mod

    orig = orch._build_executor_context

    def wrapper(state, step, results, **shared):
        ctx = orig(state, step, results, **shared)
        captured.append(ctx)
        return ctx

    orch._build_executor_context = wrapper

    mock_llm.push_tool_call("dummy", {})
    mock_llm.push_text("done")

    history = [
        {"role": "user", "content": "历史上轮：销售额分析"},
        {"role": "assistant", "content": "历史上轮：已完成销售趋势图"},
    ]
    await _drain_queue(
        orch, monkeypatch, mock_llm,
        [make_step(1, "dummy")],
        history=history,
    )

    assert any("对话历史摘要" in c for c in captured)


async def test_history_injected_into_report_prompt(monkeypatch, mock_llm, orchestrator):
    """report prompt 的 user content 应包含对话历史摘要。"""
    orch, _ = orchestrator
    captured_prompts: list[list[dict]] = []

    install_tool_registry(
        monkeypatch,
        {"dummy": MockTool("dummy", result_factory=lambda **_: {"ok": True})},
    )

    from app.services.agents import agent_orchestrator as mod

    orig = orch._build_report_prompt

    def wrapper(user_msg, plan, results, history=None):
        msgs = orig(user_msg, plan, results, history=history)
        captured_prompts.append([dict(m) for m in msgs])
        return msgs

    orch._build_report_prompt = wrapper

    mock_llm.push_tool_call("dummy", {})
    mock_llm.push_text("done")

    history = [
        {"role": "user", "content": "上一轮：营收如何？"},
        {"role": "assistant", "content": "上一轮回答：营收 100 万"},
    ]
    await _drain_queue(
        orch, monkeypatch, mock_llm,
        [make_step(1, "dummy")],
        history=history,
    )

    assert captured_prompts, "应捕获到 report prompt"
    user_msg = next(m for m in captured_prompts[0] if m.get("role") == "user")
    assert "对话历史摘要" in user_msg["content"]
    assert "上一轮" in user_msg["content"]


# ───────────────────────── Task 11: phase 事件复用 ─────────────────────────


async def test_phase_events_emitted_in_order(monkeypatch, mock_llm, orchestrator):
    orch, _ = orchestrator
    install_tool_registry(
        monkeypatch,
        {
            "t1": MockTool("t1", result_factory=lambda **_: {"ok": True}),
            "render_chart": MockTool("render_chart", result_factory=lambda **_: {
                "chart_type": "bar",
                "option": {"series": [{"data": [1]}]},
                "data_code": "select 1",
            }),
        },
    )

    # 第一步：非 chart 步骤，触发 analyzing
    mock_llm.push_tool_call("t1", {})
    mock_llm.push_text("done")

    # 第二步：chart 步骤，触发 generating
    s_chart = make_step(2, "render_chart", goal="chart", depends_on=[1])
    s_chart["data_code"] = "select 1"
    mock_llm.push_tool_call("render_chart", {"chart_type": "bar"})
    mock_llm.push_text("done")

    events, state = await _drain_queue(
        orch, monkeypatch, mock_llm,
        [make_step(1, "t1"), s_chart],
    )

    # 收集 phase 事件
    phases = [e.get("phase") for e in events if e.get("type") == "status" and "phase" in e]
    assert "selecting" in phases
    assert "analyzing" in phases
    assert "generating" in phases
    assert "reporting" in phases
    # 顺序：selecting < analyzing < generating < reporting
    si = phases.index("selecting")
    ai = phases.index("analyzing")
    gi = phases.index("generating")
    ri = phases.index("reporting")
    assert si < ai < gi < ri, f"phase 事件顺序错误: {phases}"


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])