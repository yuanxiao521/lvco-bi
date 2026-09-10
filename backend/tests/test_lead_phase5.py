"""阶段 5 兜底验证（对齐设计 §9）：

- 兜底：注入 LLM 超时 → 主导 Agent 仍产出报告，且 `done.degraded=true`。

与 `test_lead_phase4_fallback.py` 的区别：那份测的是 API 层护栏（异常回落），
这份测的是 **LeadAgent 内部**——意图/决策 LLM 超时后走确定性兜底，
但分析分支照常执行并给出报告（用户始终有输出）。
"""
import json

import pytest

from app.services.agents.lead.lead_agent import LeadAgent, LeadContext
from app.services.agents.lead.lead_tools import RunAnalysisResult


class _IntentOkDecisionTimeoutLLM:
    """第 1 次 complete（意图）返回合法 JSON；第 2 次（决策）抛超时。

    `stream_chat` 供纯回答分支兜底使用。
    """

    def __init__(self, intent_obj: dict, *, fail_from: int = 2) -> None:
        self._intent_obj = intent_obj
        self._fail_from = fail_from
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        if self.complete_calls >= self._fail_from:
            raise TimeoutError("simulated llm timeout")
        return json.dumps(self._intent_obj, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield "兜底回答"


@pytest.fixture
def fake_run_analysis(monkeypatch):
    """把 run_analysis 替换为确定性实现：产出一段报告，不碰真实编排器。"""
    calls = {"n": 0}

    async def _fake(args, *, emit, **kw):  # noqa: ANN001
        calls["n"] += 1
        await emit({"type": "status", "message": "开始分析"})
        await emit({"type": "tool_call", "name": "query_sql", "args": {"sql": "select 1"}})
        await emit({"type": "text", "content": "报告：总销售额 100 元。"})
        return RunAnalysisResult(
            success=True,
            report="报告：总销售额 100 元。",
            steps=[{"step_id": 1, "goal": "查数"}],
            blocks_added=1,
            report_source="orchestrator",
            elapsed_ms=12,
        )

    import app.services.agents.lead.lead_agent as mod

    monkeypatch.setattr(mod, "run_analysis", _fake)
    return calls


async def test_decision_timeout_still_reports_and_flags_degraded(fake_run_analysis):
    """决策 LLM 超时 → 按 needs_plan 兜底为 call_analysis → 仍出报告 + done.degraded。"""
    llm = _IntentOkDecisionTimeoutLLM(
        {"intent": "analysis", "confidence": 0.9, "needs_plan": True, "reason": "多步"}
    )
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")

    events = [ev async for ev in agent.stream("帮我做一份完整的销售分析", ctx=ctx, db_session=None)]
    types = [e["type"] for e in events]

    assert "intent" in types and "decision" in types

    intent_ev = next(e for e in events if e["type"] == "intent")
    assert intent_ev["intent"] == "analysis"
    assert intent_ev["degraded"] is False

    decision_ev = next(e for e in events if e["type"] == "decision")
    assert decision_ev["action"] == "call_analysis"
    assert decision_ev["degraded"] is True

    reports = [e for e in events if e["type"] == "report"]
    assert reports and "总销售额" in reports[-1]["content"]

    done = events[-1]
    assert done["type"] == "done"
    assert done["degraded"] is True
    assert "lead_decision_fallback" in done["degradations"]
    assert fake_run_analysis["n"] == 1


async def test_intent_timeout_falls_back_to_rule(fake_run_analysis):
    """意图 LLM 也超时 → 规则意图兜底；非分析类问题走回答分支，同样有输出。"""
    llm = _IntentOkDecisionTimeoutLLM({}, fail_from=1)  # 第 1 次即失败
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s2")

    events = [ev async for ev in agent.stream("你好，介绍一下你自己", ctx=ctx, db_session=None)]
    done = events[-1]

    intent_ev = next(e for e in events if e["type"] == "intent")
    assert intent_ev["degraded"] is True
    assert done["type"] == "done"
    assert done["degraded"] is True
    assert "lead_intent_fallback" in done["degradations"]
    # 有任意文本/报告输出，用户不会看到空白
    assert any(e["type"] in ("text", "report") for e in events)
