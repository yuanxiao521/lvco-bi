"""LLM 路由分类器测试（Task 4, P0-1）。

覆盖 agent_stream 的路由决策链路：
- LLM 分类 complex → 编排器；simple → ReAct（纠正长度启发式的误判）
- LLM 异常 / 超时 / 返回无法识别内容 → 一律默认简单任务（走 ReAct）
- AGENT_ORCHESTRATOR_ENABLED=false / phase 非 selecting → 短路不调分类器
- _classify_task_complexity 的 JSON 枚举解析（大小写/换行容错、异常、兜底 simple）

全部使用 Mock LLM 与 Fake 编排器 / ReAct，无需数据库与真实 LLM。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services.ai_service import AIService
from app.services.llm_client import LLMClient


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


async def test_classify_returns_true_for_complex_json():
    """返回 JSON {"classification": "complex"}（含大小写/换行）应解析为 True。"""
    llm = StubRouteLLM(reply='{\n  "classification": "COMPLEX"\n}')
    svc = AIService(llm=llm)
    assert await svc._classify_task_complexity("生成完整的销售分析报告") is True
    # 分类请求：system 为路由分类 prompt，user 为原消息
    assert llm.last_messages is not None
    assert llm.last_messages[0]["role"] == "system"
    assert "complex" in llm.last_messages[0]["content"]
    assert llm.last_messages[-1] == {"role": "user", "content": "生成完整的销售分析报告"}


async def test_classify_returns_false_for_simple():
    llm = StubRouteLLM(reply="simple")
    svc = AIService(llm=llm)
    assert await svc._classify_task_complexity("销售额是多少") is False


async def test_classify_returns_false_on_error_and_garbage():
    """LLM 异常或返回无法识别的内容 → 一律默认简单任务（False）。"""
    assert await AIService(llm=StubRouteLLM(error=RuntimeError("boom")))._classify_task_complexity("x") is False
    assert await AIService(llm=StubRouteLLM(reply="无法判断"))._classify_task_complexity("x") is False
    assert await AIService(llm=StubRouteLLM(reply='{"class": "complex"}'))._classify_task_complexity("x") is False


# ======================================================================
# agent_stream 路由决策
# ======================================================================


async def test_route_complex_goes_to_orchestrator(route_env):
    """LLM 判 complex → 走编排器（短复杂消息不再被长度启发式误判）。"""
    llm = StubRouteLLM(reply='{"classification": "complex"}')
    svc = AIService(llm=llm)
    events = await _run_agent(svc, COMPLEX_SHORT_MSG)
    assert llm.complete_calls == 1
    assert FakeOrchestrator.instances == 1
    assert FakeReactAgent.instances == 0
    assert any(e.get("message") == "ORCHESTRATOR_MARK" for e in events)


async def test_route_simple_goes_to_react(route_env):
    """LLM 判 simple → 走 ReAct（长简单消息不再被长度启发式误判）。"""
    llm = StubRouteLLM(reply='{"classification": "simple"}')
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert llm.complete_calls == 1
    assert FakeReactAgent.instances == 1
    assert FakeOrchestrator.instances == 0
    assert any(e.get("content") == "REACT_MARK" for e in events)


async def test_route_llm_error_defaults_to_react(route_env):
    """LLM 异常 → 默认简单任务 → ReAct（不再回退启发式）。"""
    llm = StubRouteLLM(error=RuntimeError("llm down"))
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert llm.complete_calls == 1
    assert FakeReactAgent.instances == 1
    assert FakeOrchestrator.instances == 0


async def test_route_llm_error_emits_degradation_status(route_env):
    """分类 LLM 异常 → 降级 ReAct 且显式 emit 降级原因（用户/观测端可见）。"""
    llm = StubRouteLLM(error=RuntimeError("llm down"))
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert FakeReactAgent.instances == 1
    dg = [e for e in events if e.get("degradation")]
    assert len(dg) == 1
    assert dg[0]["degradation"] == "route_classifier_llm_fallback"
    assert dg[0]["type"] == "status"


class CrashingOrchestrator(FakeOrchestrator):
    """编排器替身：execute_task 抛异常，验证显式降级到 ReAct。"""

    async def execute_task(self, **kwargs):
        raise RuntimeError("orchestrator boom")
        yield {"type": "status"}  # pragma: no cover


async def test_route_orchestrator_crash_falls_back_with_degradation(route_env):
    """编排器崩溃 → 显式降级走 ReAct，并 emit 带原因的 degradation 状态事件。"""
    route_env.setattr("app.services.agents.AgentOrchestrator", CrashingOrchestrator)
    llm = StubRouteLLM(reply='{"classification": "complex"}')
    svc = AIService(llm=llm)
    events = await _run_agent(svc, COMPLEX_SHORT_MSG)
    assert FakeOrchestrator.instances == 1
    assert FakeReactAgent.instances == 1  # 显式降级到 ReAct，而非异常中断
    dg = [e for e in events if e.get("degradation")]
    assert len(dg) == 1
    assert dg[0]["degradation"] == "orchestrator_crash"


# ======================================================================
# 注入分流：已选数据源 → 只注入该源且带字段；未选 → 全部表级摘要（无字段）
# ======================================================================


async def test_agent_stream_injection_split_selected_only(monkeypatch):
    """给 selected_datasource_id 时编排器只收到该源且带字段；不给时收到全部源但 fields 为空。"""
    from types import SimpleNamespace

    class _CaptureOrch:
        captured = None

        def __init__(self, llm, db_session, extra_plannable_tools=None):
            pass

        async def execute_task(self, **kwargs):
            _CaptureOrch.captured = kwargs.get("available_datasources")
            yield {"type": "status", "message": "OK"}

    def _mk(sid):
        return SimpleNamespace(
            id=sid, name=f"源{sid}", description="d", source_type=SimpleNamespace(value="csv"),
            schema_meta={"fields": [{"name": "amount", "data_type": "DOUBLE"}]},
            connection_config={},
        )

    class _DsRepo:
        def __init__(self, db):
            pass

        async def list_datasources(self, *a, **k):
            return [_mk("11111111-1111-1111-1111-111111111111"),
                    _mk("22222222-2222-2222-2222-222222222222")], 2

    async def _classify(self, msg, degradation=None):
        return True

    monkeypatch.setattr(settings, "AGENT_ORCHESTRATOR_ENABLED", True)
    monkeypatch.setattr(AIService, "_classify_task_complexity", _classify)
    monkeypatch.setattr("app.services.agents.AgentOrchestrator", _CaptureOrch)
    monkeypatch.setattr("app.repositories.datasource_repository.SQLAlchemyDataSourceRepository", _DsRepo)

    # 已选数据源：只注入该源、字段注入
    ai = AIService(MagicMock(spec=LLMClient))
    async for _ in ai.agent_stream(
        user_id="00000000-0000-0000-0000-000000000000",
        user_msg="分析订单",
        history=[], db_session=MagicMock(), initial_phase="selecting",
        selected_datasource_id="22222222-2222-2222-2222-222222222222",
    ):
        pass
    sel = _CaptureOrch.captured
    assert [d["id"] for d in sel] == ["22222222-2222-2222-2222-222222222222"]
    assert sel[0]["fields"] and sel[0]["fields_injected"] is True

    # 未选数据源：全部表级摘要（fields 置空，按需 list_fields）
    _CaptureOrch.captured = None
    async for _ in ai.agent_stream(
        user_id="00000000-0000-0000-0000-000000000000",
        user_msg="分析订单",
        history=[], db_session=MagicMock(), initial_phase="selecting",
    ):
        pass
    all_ds = _CaptureOrch.captured
    assert [d["id"] for d in all_ds] == ["11111111-1111-1111-1111-111111111111",
                                         "22222222-2222-2222-2222-222222222222"]
    assert all(d["fields"] == [] and d["fields_injected"] is False for d in all_ds)


async def test_route_timeout_defaults_to_react(route_env):
    """wait_for 超时（TimeoutError）→ 默认简单任务 → ReAct。"""
    async def _raise_timeout(coro, timeout=None, **kwargs):
        coro.close()  # 关闭未 await 的协程，避免 RuntimeWarning
        raise asyncio.TimeoutError()

    route_env.setattr("app.services.ai_service.asyncio.wait_for", _raise_timeout)
    llm = StubRouteLLM(reply="complex")
    svc = AIService(llm=llm)
    events = await _run_agent(svc, SIMPLE_LONG_MSG)
    assert FakeOrchestrator.instances == 0
    assert FakeReactAgent.instances == 1


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
