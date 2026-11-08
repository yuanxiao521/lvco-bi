# -*- coding: utf-8 -*-
"""步级 helpers：把三个执行内核各写一遍的三件"纯搬运"收敛到一处。

**不引入新抽象层**——这里没有"编排器"，只有三个纯函数/协程：

1. run_llm_step：一次 LLM 步调用的完整生命周期
   （observe span 的 enter/exit、文本分片累积、tool_calls 累积、
   reasoning_content 累积）。异常**不吞**——原样抛给调用方，
   由各内核按既有语义处理（react 直接冒泡；orchestrator/canvas 计 consecutive_errors）。
2. build_assistant_tool_message：带 tool_calls 的 assistant 消息
   （统一 reasoning_content 回传，DeepSeek 多轮工具调用必需）。
3. append_tool_results：tool 结果消息回写（tool_call_id 兜底 + 可选压缩）。

**行为契约（P1-B A 档的硬约束）**
- 调用方传入的 temperature / max_tokens / streaming 原样透传，三个内核各自保持现值；
  本模块不提供默认行为上的"统一调参"。
- llm.tool_call_round 的调用形态、异常传播路径、tool_call_id 兜底格式与改造前逐字节一致。
- 唯一的非逐字节差异：span.output 统一为同一份摘要（改造前只有 react 写了 output，
  且字段名不同）。这是**观测元数据**，不影响 prompt / 事件 / 数据库。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, NamedTuple

from app.services.agents.tool_executor import build_assistant_message
from app.services.context_utils import compact_result_json
from app.services.observability import observe_llm_call

logger = logging.getLogger("lvco.agents.step_helpers")


class StepLLMResult(NamedTuple):
    """一次步调用的产物。可按 (tool_calls, text_parts, reasoning_content) 直接解包。"""

    tool_calls: list[dict]
    text_parts: list[str]
    reasoning_content: str

    @property
    def text(self) -> str:
        """已累积的文本（未 strip，保持调用方原有语义）。"""
        return "".join(self.text_parts)

    @property
    def has_text(self) -> bool:
        """是否产出过文本事件（等价于旧 has_text_output 的判据）。"""
        return bool(self.text_parts)


async def run_llm_step(
    llm,
    messages: list[dict],
    tools: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    streaming: bool | None = False,
    trace=None,
    span_name: str = "llm_step",
    model: str | None = None,
    on_call: Callable[[dict], Awaitable[None]] | None = None,
    span_input: dict | None = None,
) -> StepLLMResult:
    """执行一次 LLM 步调用并累积事件。

    Args:
        llm: 必须提供 tool_call_round 的客户端（LLMClient）。
        messages/tools: 原样透传。
        temperature/max_tokens/streaming: 原样透传（各内核保持自己的现值）。
        trace: observability trace；None 时完全不建 span（与改造前一致）。
        span_name: generation 名（react_iter_N / exec_decision_N / canvas_exec_N）。
        model: 真实模型名（用于 Langfuse cost 核算）。
        on_call: 每收到一个原始事件就 await 一次（react 用它逐字 emit 文本）。
                 必须是 async 可调用；其异常同样不吞。
        span_input: 可选，覆盖 span.input（react 原来写的诊断字段，保持行为不变）。

    Returns:
        StepLLMResult(tool_calls, text_parts, reasoning_content)

    Raises:
        原样抛出 llm.tool_call_round 的异常（span 已在 finally 中收尾）。
    """
    cm = None
    span = None
    if trace is not None:
        try:
            cm = observe_llm_call(
                trace, span_name, messages=messages, model=model, temperature=temperature,
            )
            span = cm.__enter__()
        except Exception:  # noqa: BLE001 - 观测不可用绝不能影响主链路
            logger.debug("[step_helpers] observe_llm_call enter failed", exc_info=True)
            cm, span = None, None
        else:
            if span_input is not None:
                span.input = span_input

    tool_calls: list[dict] = []
    text_parts: list[str] = []
    reasoning_content = ""
    try:
        async for event in llm.tool_call_round(
            messages, tools, temperature=temperature,
            max_tokens=max_tokens, streaming=streaming,
        ):
            et = event.get("type")
            if et == "text":
                text_parts.append(event.get("content", ""))
            elif et == "tool_call":
                # DeepSeek reasoning 模式：reasoning_content 随工具调用事件下发，
                # 必须在下一轮 assistant 消息里回传，否则多轮工具调用会降质/被拒。
                rc = event.get("reasoning_content", "")
                if rc:
                    reasoning_content = rc
                tool_calls.append(event)
            if on_call is not None:
                await on_call(event)
    finally:
        if span is not None:
            try:
                span.update(output={
                    "text_length": sum(len(c) for c in text_parts),
                    "tool_calls": [{"name": t.get("name")} for t in tool_calls],
                    "has_text_output": bool(text_parts),
                })
            except Exception:  # noqa: BLE001
                logger.debug("[step_helpers] span update failed", exc_info=True)
        if cm is not None:
            try:
                # 与改造前一致：异常在调用方处理，这里不把 error 塞给 span
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("[step_helpers] span exit failed", exc_info=True)

    return StepLLMResult(
        tool_calls=tool_calls, text_parts=text_parts,
        reasoning_content=reasoning_content,
    )


def build_assistant_tool_message(
    tool_calls: list[dict],
    reasoning_content: str = "",
    *,
    id_offset: int = 0,
) -> dict:
    """带 tool_calls 的 assistant 消息（含 reasoning_content 回传）。

    薄封装：与既有 tool_executor.build_assistant_message 逐字节同构，
    存在的意义是让三个内核都从 step_helpers 一处拿，避免有人漏传 reasoning。
    """
    return build_assistant_message(
        tool_calls, reasoning_content=reasoning_content, id_offset=id_offset,
    )


def append_tool_results(
    messages: list[dict],
    results,
    *,
    id_prefix: str = "call_",
    id_offset: int = 0,
    compact: bool = True,
) -> None:
    """把一批 ToolCallResult 回写成 role=tool 消息（追加到 messages 末尾）。

    Args:
        results: 形如 ToolCallResult 的对象序列（需有 .tc / .result）。
        id_prefix/id_offset: tool_call_id 兜底（LLM 未返回 id 时使用）。
            生成规则 id_prefix + str(id_offset + i)，与改造前
            call_{tool_call_count + i} / call_{len(messages)} 完全一致。
        compact: True 时用 compact_result_json 压缩（orchestrator/react 现状）；
            False 时原样写入（canvas 现状：它的结果是 canvas_action，需要完整行数据）。

    Note:
        严格保持 tc.get("id", <fallback>) 语义——空字符串 id 不会被兜底替换
        （流式路径会下发 "id": ""，改造前后都不能误判）。
    """
    for i, pr in enumerate(results):
        tc = getattr(pr, "tc", None) or {}
        result = getattr(pr, "result", "")
        messages.append({
            "role": "tool",
            "tool_call_id": tc.get("id", f"{id_prefix}{id_offset + i}"),
            "content": compact_result_json(result) if compact else result,
        })
