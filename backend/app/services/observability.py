"""Langfuse 可观测性封装。

提供 Agent 全链路追踪能力：Trace / Span / Generation / Tool。

向后兼容：
- 当 LANGFUSE_ENABLED=false 或未配置密钥时，所有方法为 no-op。
- 调用方无需关心是否启用了 Langfuse。

使用示例：
    from app.services.observability import get_observer, observe_llm_call, observe_tool_call

    observer = get_observer()
    with observer.trace("agent_session", user_id=user_id) as trace:
        with observe_llm_call(trace, "chat", messages=msgs) as span:
            response = await llm.complete(...)
            span.update(output=response, model=model, tokens=usage)
        with observe_tool_call(trace, "query_datasource", args={"sql": sql}) as span:
            result = await tool.execute(...)
            span.update(output=result)
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import settings

logger = logging.getLogger("lvco.observability")


@dataclass
class SpanRecord:
    """轻量级 span 记录，未启用 Langfuse 时使用本地累计。"""

    name: str
    span_type: str  # "trace" | "generation" | "tool"
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    input: Any = None
    output: Any = None
    error: str | None = None

    def update(
        self,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if output is not None:
            self.output = output
        if metadata:
            self.metadata.update(metadata)
        if error:
            self.error = error

    def finish(self) -> None:
        self.end_time = time.time()

    @property
    def latency_ms(self) -> int:
        end = self.end_time or time.time()
        return int((end - self.start_time) * 1000)


@dataclass
class TraceRecord:
    """Trace 容器，子 span 通过 children 列表引用。"""

    name: str
    user_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[SpanRecord] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    _langfuse_trace: Any = None  # langfuse.Trace 对象（启用时）

    def span(self, name: str, span_type: str = "span") -> SpanRecord:
        s = SpanRecord(name=name, span_type=span_type)
        self.children.append(s)
        return s

    def finish(self) -> None:
        self.end_time = time.time()
        # 调用 Langfuse SDK flush（如果启用）
        if self._langfuse_trace is not None:
            try:
                self._langfuse_trace.update(
                    output={
                        "children_count": len(self.children),
                        "latency_ms": self.latency_ms,
                    }
                )
            except Exception:
                logger.debug("langfuse trace update failed", exc_info=True)

    @property
    def latency_ms(self) -> int:
        end = self.end_time or time.time()
        return int((end - self.start_time) * 1000)


# ------------------------------------------------------------------
# Langfuse 集成层（条件导入，未启用时为 None）
# ------------------------------------------------------------------

_langfuse_client: Any | None = None


def _get_langfuse_client() -> Any | None:
    """按需初始化 Langfuse 客户端。失败时降级到本地模式。"""
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client
    if not settings.is_langfuse_configured:
        return None
    try:
        from langfuse import Langfuse  # type: ignore

        _langfuse_client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )
        logger.info("langfuse_initialized host=%s", settings.LANGFUSE_HOST)
        return _langfuse_client
    except Exception as e:
        logger.warning("langfuse_init_failed fallback=local error=%s", e)
        return None


# ------------------------------------------------------------------
# Observer 入口
# ------------------------------------------------------------------


class Observer:
    """统一的可观测性入口，未启用 Langfuse 时降级为本地日志。"""

    def __init__(self) -> None:
        self._client = _get_langfuse_client()

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @contextmanager
    def trace(
        self,
        name: str,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[TraceRecord]:
        """开启一次 trace。返回 TraceRecord，with 块结束自动 finish。"""
        rec = TraceRecord(
            name=name,
            user_id=user_id,
            session_id=session_id,
            metadata=metadata or {},
        )

        if self._client is not None:
            try:
                rec._langfuse_trace = self._client.trace(
                    name=name,
                    user_id=user_id,
                    session_id=session_id,
                    metadata=metadata or {},
                )
            except Exception as e:
                logger.debug("langfuse trace create failed: %s", e)

        try:
            yield rec
        except Exception as e:
            rec.metadata["exception"] = str(e)
            raise
        finally:
            rec.finish()
            if not self.enabled:
                logger.info(
                    "trace_local name=%s latency_ms=%d children=%d",
                    rec.name,
                    int((rec.end_time - rec.start_time) * 1000),
                    len(rec.children),
                )

    def flush(self) -> None:
        """强制刷新（建议在请求结束时调用）。"""
        if self._client is not None:
            try:
                self._client.flush()
            except Exception:
                logger.debug("langfuse flush failed", exc_info=True)


# 单例
_observer: Observer | None = None


def get_observer() -> Observer:
    """获取 Observer 单例。"""
    global _observer
    if _observer is None:
        _observer = Observer()
    return _observer


# ------------------------------------------------------------------
# 便捷装饰器/上下文
# ------------------------------------------------------------------


@contextmanager
def observe_llm_call(
    trace: TraceRecord,
    name: str,
    *,
    messages: list[dict] | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> Iterator[SpanRecord]:
    """记录一次 LLM 调用 span。"""
    span = trace.span(name=f"llm:{name}", span_type="generation")
    span.input = {"messages": messages, "model": model, "temperature": temperature}

    langfuse_generation = None
    if trace._langfuse_trace is not None:
        try:
            langfuse_generation = trace._langfuse_trace.generation(
                name=name,
                model=model,
                input=messages,
                metadata={"temperature": temperature} if temperature else None,
            )
        except Exception:
            logger.debug("langfuse generation create failed", exc_info=True)

    try:
        yield span
    except Exception as e:
        span.update(error=str(e))
        if langfuse_generation is not None:
            try:
                langfuse_generation.update(level="ERROR", status_message=str(e))
            except Exception:
                pass
        raise
    finally:
        span.finish()
        if langfuse_generation is not None:
            try:
                langfuse_generation.update(
                    output=span.output,
                    metadata={"latency_ms": span.latency_ms, **(span.metadata)},
                )
                langfuse_generation.end()
            except Exception:
                logger.debug("langfuse generation end failed", exc_info=True)


@contextmanager
def observe_tool_call(
    trace: TraceRecord,
    tool_name: str,
    *,
    args: dict[str, Any] | None = None,
) -> Iterator[SpanRecord]:
    """记录一次 tool 调用 span。"""
    span = trace.span(name=f"tool:{tool_name}", span_type="tool")
    span.input = args or {}

    langfuse_span = None
    if trace._langfuse_trace is not None:
        try:
            langfuse_span = trace._langfuse_trace.span(
                name=f"tool:{tool_name}",
                input=args,
                metadata={"tool_name": tool_name},
            )
        except Exception:
            logger.debug("langfuse tool span create failed", exc_info=True)

    try:
        yield span
    except Exception as e:
        span.update(error=str(e))
        if langfuse_span is not None:
            try:
                langfuse_span.update(level="ERROR", status_message=str(e))
            except Exception:
                pass
        raise
    finally:
        span.finish()
        if langfuse_span is not None:
            try:
                langfuse_span.update(
                    output=span.output,
                    metadata={"latency_ms": span.latency_ms, **(span.metadata)},
                )
                langfuse_span.end()
            except Exception:
                logger.debug("langfuse tool span end failed", exc_info=True)