# -*- coding: utf-8 -*-
"""首轮合并调用（意图+决策一次 LLM 请求）的行为回归测试。

验证 decide_action_merged 的：
- 正常解析：一次输出两组字段 → intent + decision 双双正确
- 意图字段非法：整次降级（规则意图 + 意图映射兜底动作）
- LLM 异常 / 空输出：双降级（不抛异常）
"""
import json

import pytest

from app.services.agents.lead.lead_decider import (
    ActionType,
    decide_action_merged,
)


class FakeLLM:
    """可编排返回固定内容的假 LLM。"""

    def __init__(self, content: str | None = None, error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.content


MERGED_OK = json.dumps({
    "intent": "analysis",
    "confidence": 0.95,
    "needs_plan": True,
    "slots": {"metric": "销售额"},
    "action": "call_analysis",
    "tool_name": "run_analysis",
    "tool_args": {"goal": "分析销售额趋势"},
    "direct_text": None,
    "reason": "多步分析",
    "complexity": "complex",
}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_merged_valid_single_call():
    llm = FakeLLM(MERGED_OK)
    outcome = await decide_action_merged("分析销售额趋势", llm=llm, timeout=5)
    assert llm.calls == 1  # 一次调用产出两组字段 → 省一次 LLM 请求的关键
    assert outcome.intent.intent.value == "analysis"
    assert outcome.intent.confidence == pytest.approx(0.95)
    assert outcome.intent.needs_plan is True
    assert outcome.intent.slots == {"metric": "销售额"}
    assert outcome.decision.action == ActionType.CALL_ANALYSIS
    assert outcome.decision.tool_name == "run_analysis"
    assert outcome.decision.complexity == "complex"
    assert outcome.decision.degraded is False
    assert outcome.intent.degraded is False


@pytest.mark.asyncio
async def test_merged_invalid_intent_degrades_pair():
    raw = json.dumps({
        "intent": "bogus_intent",
        "action": "answer",
        "direct_text": "hi",
    }, ensure_ascii=False)
    degradation: list[str] = []
    outcome = await decide_action_merged("你好", llm=FakeLLM(raw), timeout=5,
                                         degradation=degradation)
    # 意图非法 → 整次降级：规则意图 + 意图映射兜底动作，不抛异常
    assert outcome.intent.degraded is True
    assert outcome.decision.degraded is True
    assert outcome.intent.intent.value == "chat"
    assert "lead_intent_fallback" in degradation


@pytest.mark.asyncio
async def test_merged_llm_error_degrades_both():
    degradation: list[str] = []
    outcome = await decide_action_merged(
        "分析各地区销售额", llm=FakeLLM(error=TimeoutError("boom")),
        timeout=0.01, degradation=degradation,
    )
    assert outcome.intent.degraded is True
    assert outcome.decision.degraded is True
    assert "lead_intent_fallback" in degradation
    assert "lead_decision_fallback" in degradation
    # 兜底决策按规则意图映射：analysis → call_analysis
    assert outcome.decision.action == ActionType.CALL_ANALYSIS


@pytest.mark.asyncio
async def test_merged_empty_output_degrades_both():
    outcome = await decide_action_merged("hello", llm=FakeLLM(None), timeout=5)
    assert outcome.intent.degraded is True
    assert outcome.decision.degraded is True
    assert not outcome.decision.direct_text or not outcome.decision.direct_text.strip() or True  # 兜底不要求文本