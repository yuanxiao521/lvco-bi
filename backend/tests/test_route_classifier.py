"""LLM 路由分类器测试（Task 4, P0-1）。

覆盖 agent_stream 的路由决策链路：
- LLM 分类 complex → 编排器；simple → ReAct（纠正长度启发式的误判）
- LLM 异常 / 超时 → 回退原启发式（短消息→ReAct，长消息→编排器）
- AGENT_ORCHESTRATOR_ENABLED=false / phase 非 selecting → 短路不调分类器
- _classify_task_complexity 的文本解析（大小写/换行容错、异常、不可解析）

全部使用 Mock LLM 与 Fake 编排器 / ReAct，无需数据库与真实 LLM。
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.ai_service import AIService


# ======================================================================
# Mock 基础设施
# ======================================================================


class StubRouteLLM:
    """路由分类器 LLM Stub：可配置返回文本或抛异常，记录调用。"""

    def __init__(self, reply: str = "", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.complete_calls = 0
        self.last_messages: list[dict] | None = None

    async def complete(self, messages, **kwargs):
        self.complete_calls += 1
        self.last_messages = messages
        if self.error is not None:
            raise self.error
        return self.reply


class FakeOrchestrator:
    """编排器替身：记录实例化并 yield 一个标记事件。"""

    instances = 0

    def __init__(self, llm, db_session, extra_plannable_tools=None):
        FakeOrchestrator.instances += 1

    async def execute_task(self, **kwargs):
        yield {"type": "status", "message": "ORCHESTRATOR_MARK"}


class FakeReactAgent:
    """ReAct 替身：记录实例化并 emit 一个标记事件。"""

    instances = 0

    def __init__(self, llm, tools, trace):
        FakeReactAgent.instances += 1

    async def run(self, *, messages, user_id, db_session, initial_phase, emit):
        await emit({"type": "text", "content": "REACT_MARK"})


@pytest.fixture
def route_env(monkeypatch):
    """路由测试环境：mock 编排器 / ReAct，重置实例计数。"""
    FakeOrchestrator.instances = 0
    FakeReactAgent.instances = 0
    monkeypatch.setattr("app.services.agents.AgentOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("app.services.agents.react_agent.ReactGraphAgent", FakeReactAgent)
    return monkeypatch


async def _run_agent(service: AIService, user_msg: str, phase: str = "selecting") -> list[dict]:
    """跑一遍 agent_stream 并收集事件。user_id 用非法 UUID 让编排路径跳过数据源加载。"""
    events: list[dict] = []
    async for ev in service.agent_stream(
        user_id="u1",
        user_msg=user_msg,
        history=[],
        db_session=None,
        initial_phase=phase,
    ):
        events.append(ev)
    return events


# 短消息（≤20 字）但语义复杂：原启发式误判为 simple，LLM 分类应纠正
COMPLEX_SHORT_MSG = "对比A、B两个产品的销售趋势"
# 长消息（>20 字）且非"列出/有哪些"开头：原启发式会走编排器
SIMPLE_LONG_MSG = "请帮我分析一下今年各个月份的整体销售表现情况并给出总结"


# ======================================================================
# _classify_task_complexity 直测
# ======================================================================


async def test_classify_returns_true_for_complex_with_case_and_newline():
    """返回 "COMPLEX\n"（大写+换行）应解析为 True。"""
    llm = StubRouteLLM(reply="COMPLEX\n")
    svc = AIService(llm=llm)
    assert await svc._classify_task_complexity("生成完整的销售分析报告") is True
    # 分类请求：system 为路由分类 prompt，user 为原消息
    assert llm.last_messages is not None
    assert llm.last_messages[0]["role"] == "system"
    assert "complex" in llm.last_messages[0]["content"]
    assert llm.last_messages[-1] == {"role": "user", "content": "生成完整的销售分析报告"}


async def test_classify_returns_false_for_simple():
    llm = StubRouteLLM(reply="  simple  ")
    svc = AIService(llm=llm)
    assert await svc._classify_task_complexity("销售额是多少") is False


async def test_classify_returns_none_on_error_and_garbage():
    assert await AIService(llm=StubRouteLLM(error=RuntimeError("boom")))._classify_task_complexity("x") is None
    assert await AIService(llm=StubRouteLLM(reply="无法判断"))._classify_task_complexity("x") is None


# ======================================================================
# agent_stream 路由决策
# ======================================================================


async def test_route_complex_goes_to_orchestrator(route_env):
    """LLM 判 complex → 走编排器（短复杂消息不再被长度启发式误判）。"""
    llm = StubRouteLLM(reply="complex")
    svc = AIService(llm=llm)
    events = await _run_agent(svc, COMPLEX_SHORT_MSG)
    assert llm.complete_calls == 1
    assert FakeOrchestrator.instances == 1
    assert FakeReactAgent.instances == 0
    assert any(e.get("message") == "ORCHESTRATOR_MARK" for e in events)


async def test_route_simple_goes_to_react(route_env):
    """LLM 判 simple → 走 ReAct（长简单消息不再被长度启发式误判）。"""
    llm = StubRouteLLM(reply="simple")
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert llm.complete_calls == 1
    assert FakeReactAgent.instances == 1
    assert FakeOrchestrator.instances == 0
    assert any(e.get("content") == "REACT_MARK" for e in events)


async def test_route_llm_error_falls_back_to_heuristic_short_msg(route_env):
    """LLM 异常 + 短消息 → 回退启发式 → ReAct。"""
    llm = StubRouteLLM(error=RuntimeError("llm down"))
    svc = AIService(llm=llm)
    events = await _run_agent(svc, "销售额是多少")
    assert llm.complete_calls == 1
    assert FakeReactAgent.instances == 1
    assert FakeOrchestrator.instances == 0


async def test_route_llm_error_falls_back_to_heuristic_long_msg(route_env):
    """LLM 异常 + 长消息（>20 字且非列出/有哪些开头）→ 回退启发式 → 编排器。"""
    llm = StubRouteLLM(error=RuntimeError("llm down"))
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert FakeOrchestrator.instances == 1
    assert FakeReactAgent.instances == 0


async def test_route_timeout_falls_back_to_heuristic(route_env):
    """wait_for 超时（TimeoutError）→ 回退启发式（长消息 → 编排器）。"""
    async def _raise_timeout(coro, timeout=None, **kwargs):
        coro.close()  # 关闭未 await 的协程，避免 RuntimeWarning
        raise asyncio.TimeoutError()

    route_env.setattr("app.services.ai_service.asyncio.wait_for", _raise_timeout)
    llm = StubRouteLLM(reply="complex")
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert FakeOrchestrator.instances == 1
    assert FakeReactAgent.instances == 0


async def test_route_disabled_skips_classifier(route_env):
    """AGENT_ORCHESTRATOR_ENABLED=false → 短路，不调分类器，直接走 ReAct。"""
    route_env.setattr("app.config.settings.AGENT_ORCHESTRATOR_ENABLED", False)
    llm = StubRouteLLM(reply="complex")
    svc = AIService(llm=llm)
    events = await _run_agent(svc, COMPLEX_SHORT_MSG)
    assert llm.complete_calls == 0
    assert FakeOrchestrator.instances == 0
    assert FakeReactAgent.instances == 1


async def test_route_non_selecting_phase_skips_classifier(route_env):
    """phase 非 selecting → 短路，不调分类器，直接走 ReAct。"""
    llm = StubRouteLLM(reply="complex")
    svc = AIService(llm=llm)
    events = await _run_agent(svc, COMPLEX_SHORT_MSG, phase="analyzing")
    assert llm.complete_calls == 0
    assert FakeOrchestrator.instances == 0
    assert FakeReactAgent.instances == 1
