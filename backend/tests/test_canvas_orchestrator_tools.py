"""画布工具按入口注入编排器：Planner 白名单合并 + Orchestrator/agent_stream 透传 + AIChat 不注入。

验证 spec: canvas-orchestrator-tools
- 画布入口可规划 5 个画布工具；普通对话入口（不注入）不可规划。
"""
import uuid
from unittest.mock import MagicMock

from app.config import settings
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.agent_orchestrator import AgentOrchestrator
from app.services.ai_service import AIService
from app.services.canvas_tools import CANVAS_TOOL_NAMES
from app.services.llm_client import LLMClient


def _plan_with_step(tool: str) -> dict:
    return {
        "task_summary": "在画布搭报告",
        "steps": [{
            "step_id": 1,
            "goal": "在画布新增一个图",
            "tool": tool,
            "depends_on": [],
            "purpose": "p",
        }],
        "expected_output": "report",
    }


# ---------- Planner：白名单合并 ----------

def test_planner_with_extra_allows_canvas_tool():
    """注入后，add_chart_block 步骤通过白名单。"""
    p = PlannerAgent(MagicMock(spec=LLMClient), extra_plannable_tools=CANVAS_TOOL_NAMES)
    plan = p._normalize_plan(_plan_with_step("add_chart_block"))
    assert any(s["tool"] == "add_chart_block" for s in plan["steps"])


def test_planner_without_extra_filters_canvas_tool():
    """不注入（普通对话），add_chart_block 步骤被白名单过滤。"""
    p = PlannerAgent(MagicMock(spec=LLMClient))
    plan = p._normalize_plan(_plan_with_step("add_chart_block"))
    assert all(s["tool"] != "add_chart_block" for s in plan["steps"])


def test_planner_still_allows_safe_tools_without_extra():
    """既有 orchestrator_safe 工具不受影响。"""
    p = PlannerAgent(MagicMock(spec=LLMClient))
    plan = p._normalize_plan(_plan_with_step("list_datasources"))
    assert any(s["tool"] == "list_datasources" for s in plan["steps"])


# ---------- Orchestrator：透传 ----------

def test_orchestrator_wires_extra_tools_to_planner():
    o = AgentOrchestrator(MagicMock(spec=LLMClient), MagicMock(), extra_plannable_tools=CANVAS_TOOL_NAMES)
    assert o.extra_plannable_tools == set(CANVAS_TOOL_NAMES)
    assert o.planner.extra_plannable_tools == set(CANVAS_TOOL_NAMES)


def test_orchestrator_default_has_no_extra_tools():
    o = AgentOrchestrator(MagicMock(spec=LLMClient), MagicMock())
    assert o.extra_plannable_tools == set()
    assert o.planner.extra_plannable_tools == set()


# ---------- agent_stream：入口参数透传到 Orchestrator ----------

class _FakeOrchestrator:
    """记录构造参数并空转结束的桩编排器。"""
    captured: object = None

    def __init__(self, llm, db, extra_plannable_tools=None):
        _FakeOrchestrator.captured = extra_plannable_tools

    async def execute_task(self, **kwargs):  # noqa: B027
        return
        yield  # pragma: no cover


async def test_agent_stream_passes_extra_tools_to_orchestrator(monkeypatch):
    """画布入口：extra_plannable_tools 从 agent_stream 传到编排器。"""
    import app.services.ai_service as service_module

    async def _classify(self, msg):  # noqa: ANN001
        return True

    monkeypatch.setattr(settings, "AGENT_ORCHESTRATOR_ENABLED", True)
    monkeypatch.setattr(AIService, "_classify_task_complexity", _classify)
    monkeypatch.setattr("app.services.agents.AgentOrchestrator", _FakeOrchestrator)

    ai = AIService(MagicMock(spec=LLMClient))
    async for _ in ai.agent_stream(
        user_id=str(uuid.uuid4()),
        user_msg="在画布做一份按渠道对比的营销分析报告并排好版，之后总结结论",
        history=[], db_session=MagicMock(), initial_phase="selecting",
        extra_plannable_tools=CANVAS_TOOL_NAMES,
    ):
        pass

    assert _FakeOrchestrator.captured == CANVAS_TOOL_NAMES
    _FakeOrchestrator.captured = None


async def test_agent_stream_default_passes_none_to_orchestrator(monkeypatch):
    """普通对话：不传 extra_plannable_tools（同为 None）→ 不注入画布工具。"""
    import app.services.ai_service as service_module  # noqa: F401

    async def _classify(self, msg):  # noqa: ANN001
        return True

    monkeypatch.setattr(settings, "AGENT_ORCHESTRATOR_ENABLED", True)
    monkeypatch.setattr(AIService, "_classify_task_complexity", _classify)
    monkeypatch.setattr("app.services.agents.AgentOrchestrator", _FakeOrchestrator)

    ai = AIService(MagicMock(spec=LLMClient))
    async for _ in ai.agent_stream(
        user_id=str(uuid.uuid4()),
        user_msg="对比一下各产品线的月度销售额走势，并给出归因分析结论",
        history=[], db_session=MagicMock(), initial_phase="selecting",
    ):
        pass

    assert _FakeOrchestrator.captured is None
    _FakeOrchestrator.captured = None