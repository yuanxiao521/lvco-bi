# -*- coding: utf-8 -*-
"""工具调用的流式作用域：**给人看的流式，给程序用的非流式**。

背景：执行器的流式文字并非都展示给用户——
  ReactAgent         工具循环末轮产出**用户要看的报告** → 保留流式（打字机）
  AgentOrchestrator  中间文字只攒进 text_parts，报告末尾整块 emit → 流式零收益
  CanvasOrchestrator 同上
所以按调用点显式指定 streaming，而非一刀切。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.services.llm_client import LLMClient


def _run(coro):
    return asyncio.run(coro)


class _ProbeLLM(LLMClient):
    """只探测「走了流式还是非流式」，不碰网络。"""

    def __init__(self, default: bool) -> None:
        # 跳过父类 __init__（此处不需要真实配置/密钥）
        self._settings = SimpleNamespace(LLM_TOOL_STREAMING=default)
        self.calls: list[str] = []

    async def stream_chat_with_tools(self, messages, tools, *, temperature=0.3, max_tokens=None):
        self.calls.append("stream")
        yield {"type": "text", "content": "S"}
        yield {"type": "done"}

    async def complete_with_tools(self, messages, tools, *, temperature=0.3, max_tokens=None, timeout=None):
        self.calls.append("complete")
        return {"content": "C", "tool_calls": []}


def _drain(llm, **kw) -> list[dict]:
    async def main():
        return [ev async for ev in llm.tool_call_round([], [], **kw)]

    return _run(main())


def test_default_follows_global_flag_true():
    llm = _ProbeLLM(default=True)
    _drain(llm)
    assert llm.calls == ["stream"]


def test_default_follows_global_flag_false():
    llm = _ProbeLLM(default=False)
    _drain(llm)
    assert llm.calls == ["complete"]


def test_explicit_true_overrides_global_false():
    """ReactAgent 的用法：全局关掉流式，它仍要流式（报告给用户看）。"""
    llm = _ProbeLLM(default=False)
    events = _drain(llm, streaming=True)
    assert llm.calls == ["stream"]
    assert any(e.get("content") == "S" for e in events)


def test_explicit_false_overrides_global_true():
    """两个编排器的用法：全局开着流式，它们仍走非流式（省掉分片拼接）。"""
    llm = _ProbeLLM(default=True)
    events = _drain(llm, streaming=False)
    assert llm.calls == ["complete"]
    # 非流式一次性给出全部文本
    assert [e["content"] for e in events if e.get("type") == "text"] == ["C"]
    assert events[-1] == {"type": "done"}


def _src(name: str) -> str:
    base = Path(__file__).resolve().parents[1] / "app" / "services" / "agents"
    return (base / name).read_text(encoding="utf-8")


def test_executors_pass_explicit_streaming():
    """三个执行器的流式策略必须显式钉住，防止被无意改回一刀切。"""
    assert "streaming=True" in _src("react_agent.py"), "ReAct 必须保持流式（报告给用户看）"
    assert "streaming=False" in _src("agent_orchestrator.py"), "通用编排器应走非流式"
    assert "streaming=False" in _src("canvas_orchestrator.py"), "画布编排器应走非流式"
