"""ReAct 路径防失控专项测试：
1. 单轮并发工具调用钳制（MAX_PARALLEL_TOOL_CALLS）
2. 累计查询次数上限强制收尾（MAX_QUERY_CALLS）
3. 空输出兜底：LLM 无工具调用且无文本时强制生成报告
"""
from __future__ import annotations

import json
from unittest.mock import patch

from app.services.agent_tools import ToolRegistry
from app.services.agents.react_agent import ReactGraphAgent
from app.services.agents.tool_executor import ToolCallResult


class StubLLM:
    """模拟 LLM：按轮次返回 tool_calls；轮次耗尽后返回 final_round 文本（或空=触发兜底）。"""

    def __init__(self, rounds: list[list[dict]], final_round: str = "正常最终回答"):
        self.rounds_calls = rounds
        self.final_round = final_round
        self.round = 0
        self.received_tools: list[list[str]] = []
        self.complete_calls = 0
        self.complete_reply = "## 兜底报告\n根据已有查询结果生成。"
        self.last_complete_prompt: list[dict] = []

    async def stream_chat_with_tools(self, messages, tools, **kw):
        self.received_tools.append([t["function"]["name"] for t in tools])
        self.round += 1
        if self.round <= len(self.rounds_calls):
            for tc in self.rounds_calls[self.round - 1]:
                yield tc
        elif self.final_round:
            yield {"type": "text", "content": self.final_round}
        # 否则：什么都不产出（模拟静默收尾）

    async def complete(self, messages, **kw):
        self.complete_calls += 1
        self.last_complete_prompt = messages
        return self.complete_reply


class StubExecutor:
    instances = 0
    force_error = False  # 置 True 模拟查询失败（has_error → phase 不流转 → 触发查询上限兜底）

    def __init__(self, **kw):
        StubExecutor.instances += 1

    async def execute_tool_call(self, tc, args=None):
        result = (
            json.dumps({"error": "query failed", "hint": "check columns"}, ensure_ascii=False)
            if StubExecutor.force_error
            else json.dumps({"columns": ["a"], "rows": [{"a": 1}], "summary": {"rows_count": 1}}, ensure_ascii=False)
        )
        return ToolCallResult(
            tc=tc,
            name=tc.get("name", ""),
            args=args or {},
            result=result,
            is_error=StubExecutor.force_error,
            fatal=False,
        )


def _mk_call(name: str, i: int) -> dict:
    return {"type": "tool_call", "id": f"call_{i}", "name": name, "arguments": '{}'}


def _tools() -> list[dict]:
    from app.services.canvas_tools import CANVAS_TOOL_NAMES
    return [t for t in ToolRegistry.schemas() if t["function"]["name"] not in CANVAS_TOOL_NAMES]


async def _run(agent: ReactGraphAgent, llm: StubLLM) -> dict:
    events: list[str] = []

    async def emit(ev: dict) -> None:
        events.append(ev.get("type"))

    state = await agent.run(
        messages=[{"role": "user", "content": "分析销售额趋势"}],
        user_id="u1",
        db_session=None,
        initial_phase="analyzing",
        emit=emit,
    )
    state["_events"] = events
    return state


async def test_parallel_tool_calls_limited():
    """单轮并发钳制：LLM 第 1 轮返回 5 个调用，只执行前 3 个。"""
    llm = StubLLM([[_mk_call("query_engine", i) for i in range(5)]])
    agent = ReactGraphAgent(llm, _tools(), None)
    with patch("app.services.agents.react_agent.ToolExecutor", StubExecutor):
        state = await _run(agent, llm)
    executed = state.get("executed_tool_names") or []
    assert len(executed) == 3, f"应钳制为 3 个，实际 {len(executed)}"


async def test_out_of_phase_tools_blocked_at_execution_layer():
    """执行层白名单：GENERATING 阶段 LLM 编造 query_sql 调用 → 不执行，注入纠正。"""
    # 第1轮 3 个 query_engine（ANALYZING 正常执行并流转 GENERATING）
    # 第2轮 LLM 无视 schema 返回 query_sql → 应被执行层拦截，不产生 executed 记录
    llm = StubLLM(
        [
            [_mk_call("query_engine", 1), _mk_call("query_engine", 2), _mk_call("query_engine", 3)],
            [_mk_call("query_sql", 4)],  # GENERATING 阶段伪造查询
        ],
        final_round="正常收尾",
    )
    agent = ReactGraphAgent(llm, _tools(), None)
    with patch("app.services.agents.react_agent.ToolExecutor", StubExecutor):
        state = await _run(agent, llm)
    names = state.get("executed_tool_names") or []
    assert names.count("query_engine") == 3
    assert "query_sql" not in names, "GENERATING 阶段的 query_sql 不应被执行"


async def test_invalid_tool_streak_forces_wrapup():
    """连续无效调用强制收尾：GENERATING 阶段两次编造 query_engine → 直接 wrapup，不再空转。"""
    llm = StubLLM(
        [
            [_mk_call("query_engine", 1), _mk_call("query_engine", 2), _mk_call("query_engine", 3)],
            [_mk_call("query_sql", 4)],  # 首次无效（streak=1，注入纠正重试）
            [_mk_call("query_engine", 5)],  # 第二次无效（streak=2 → 强制 wrapup）
        ],
        final_round="",  # 不该走到这一轮
    )
    agent = ReactGraphAgent(llm, _tools(), None)
    with patch("app.services.agents.react_agent.ToolExecutor", StubExecutor):
        state = await _run(agent, llm)
    assert state.get("done_reason") == "invalid_tool_wrapup"
    assert "text" in state["_events"], "wrapup 应产出报告文本"


async def test_silent_reason_wrapup_report():
    """空输出兜底：查询后 LLM 既无工具也无文本 → 强制生成报告文本并 emit。"""
    llm = StubLLM([[_mk_call("query_engine", 1)]], final_round="")
    agent = ReactGraphAgent(llm, _tools(), None)
    with patch("app.services.agents.react_agent.ToolExecutor", StubExecutor):
        state = await _run(agent, llm)
    assert "text" in state["_events"], "兜底应产出 text 事件"
    assert llm.complete_calls == 1, "应调用一次 complete 生成收尾报告"
    assert "分析报告" in str(llm.last_complete_prompt[0]["content"]) or "数据分析" in str(llm.last_complete_prompt[0]["content"])


async def test_silent_reason_fallback_apology_when_llm_fails():
    """LLM 兜底也失败（返回空）→ 仍输出道歉文案，保证终态非空。"""
    class StubLLMFail(StubLLM):
        async def complete(self, messages, **kw):
            self.complete_calls += 1
            return ""

    llm = StubLLMFail([[_mk_call("query_engine", 1)]], final_round="")
    agent = ReactGraphAgent(llm, _tools(), None)
    with patch("app.services.agents.react_agent.ToolExecutor", StubExecutor):
        state = await _run(agent, llm)
    assert "text" in state["_events"], "兜底失败仍应输出道歉文案"
    assert llm.complete_calls == 1