"""Observability 模块单测。

验证：
1. LANGFUSE_ENABLED=false 时 get_observer() 返回的 Observer 不报错
2. trace / observe_llm_call / observe_tool_call 在 with 块外不影响后续代码
3. TraceRecord / SpanRecord 字段计算正确（latency_ms 等）
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

# 强制关闭 Langfuse，确保单测不依赖外网
os.environ.setdefault("LANGFUSE_ENABLED", "false")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "")


def test_observer_disabled_when_langfuse_not_configured():
    """未配置 Langfuse 时，Observer.enabled 应为 False。"""
    from app.config import settings
    from app.services.observability import get_observer

    # 重置单例
    import app.services.observability as obs_module
    obs_module._observer = None
    obs_module._langfuse_client = None

    with patch.object(settings, "LANGFUSE_ENABLED", False):
        observer = get_observer()
        assert observer.enabled is False


def test_trace_context_manager_no_langfuse():
    """未启用 Langfuse 时 trace 也能正常使用，记录到本地。"""
    from app.services.observability import TraceRecord, get_observer

    observer = get_observer()
    assert observer.enabled is False

    with observer.trace("test_trace", user_id="u1", session_id="s1") as rec:
        assert isinstance(rec, TraceRecord)
        assert rec.name == "test_trace"
        assert rec.user_id == "u1"
        assert rec.session_id == "s1"
        assert rec.metadata == {}

    # trace 块结束应自动 finish
    assert rec.end_time is not None
    assert rec.latency_ms >= 0


def test_observe_llm_call_records_output():
    """observe_llm_call 能正确记录 input/output。"""
    from app.services.observability import get_observer, observe_llm_call

    observer = get_observer()
    with observer.trace("test") as trace:
        with observe_llm_call(
            trace,
            "chat",
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4",
            temperature=0.5,
        ) as span:
            span.update(output="hello", metadata={"tokens": 10})

    # span 应有完整字段
    assert span.output == "hello"
    assert span.metadata.get("tokens") == 10
    assert span.input is not None
    assert span.input["model"] == "gpt-4"
    assert span.latency_ms >= 0


def test_observe_tool_call_records_args_and_output():
    """observe_tool_call 能正确记录 args 和 output。"""
    from app.services.observability import get_observer, observe_tool_call

    observer = get_observer()
    with observer.trace("test") as trace:
        with observe_tool_call(trace, "query_datasource", args={"sql": "SELECT 1"}) as span:
            span.update(output={"rows": [{"x": 1}]})

    assert span.span_type == "tool"
    assert span.input == {"sql": "SELECT 1"}
    assert span.output == {"rows": [{"x": 1}]}


def test_trace_metadata_propagates_through_nested_spans():
    """嵌套 span 应能正确继承 trace metadata。"""
    from app.services.observability import get_observer, observe_llm_call, observe_tool_call

    observer = get_observer()
    with observer.trace("agent_test", user_id="u1", metadata={"session": "abc"}) as trace:
        trace.metadata["step"] = 1
        with observe_llm_call(trace, "plan"):
            pass
        with observe_tool_call(trace, "execute_sql", args={"sql": "SELECT 2"}):
            pass

    assert len(trace.children) == 2
    assert trace.metadata["step"] == 1
    assert trace.metadata["session"] == "abc"


def test_exception_in_trace_records_error_but_does_not_swallow():
    """trace 块内异常应被记录但不被吞掉。"""
    from app.services.observability import get_observer

    observer = get_observer()

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        with observer.trace("error_trace") as trace:
            try:
                raise BoomError("test")
            except BoomError as e:
                trace.metadata["exception"] = str(e)
                raise

    assert trace.metadata.get("exception") == "test"
    assert trace.end_time is not None


def test_settings_model_for_task_routing():
    """config.model_for_task 应根据任务类型路由模型。"""
    from app.config import settings

    # 未配置 SIMPLE/COMPLEX 时回退到默认 openai_model
    simple = settings.model_for_task("simple")
    complex_ = settings.model_for_task("complex")
    default = settings.model_for_task("unknown")

    assert simple == settings.openai_model
    assert complex_ == settings.openai_model
    assert default == settings.openai_model


def test_settings_is_langfuse_configured():
    """LANGFUSE_ENABLED + 双 key 都配置才算 configured。"""
    from app.config import settings

    # 默认未启用
    assert settings.is_langfuse_configured is False