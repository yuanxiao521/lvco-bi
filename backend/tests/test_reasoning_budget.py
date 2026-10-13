# -*- coding: utf-8 -*-
"""推理模型的输出预算回归（不依赖 DB/Redis）。

背景：deepseek-v4.1-flash 这类**推理模型**会先输出 reasoning_content 再输出正文，
两者**共用** max_tokens。旧值（决策 400 / 意图 300）在复杂 prompt 下会被推理吃光 →
JSON 截断 → extract_json_object 返回 None → 退化成确定性兜底
（实测出现 [lead_decider] empty_or_unparsable fallback=deterministic）。

本测试锁住两件事：
1. 决策/意图调用的 max_tokens 取自配置，且预算给推理留了空间；
2. JSON 被截断时仍走确定性兜底（降级路径不被破坏）。
"""
from __future__ import annotations

import asyncio
import json

from app.config import settings
from app.services.agents.lead.lead_intent import IntentResult, IntentType


def _run(coro):
    return asyncio.run(coro)


class _CaptureLLM:
    """记录每次调用的 kwargs，返回预设内容。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.kwargs: list[dict] = []

    async def complete(self, messages, **kw):
        self.kwargs.append(kw)
        return self.reply


def test_budgets_leave_room_for_reasoning():
    """预算必须显著高于历史旧值（决策 400 / 意图 300），给推理留空间。"""
    assert settings.LEAD_DECISION_MAX_TOKENS >= 800
    assert settings.LEAD_MERGED_MAX_TOKENS >= 1000
    assert settings.LEAD_INTENT_MAX_TOKENS >= 600


def test_decide_action_uses_configured_budget():
    from app.services.agents.lead.lead_decider import decide_action

    llm = _CaptureLLM(json.dumps(
        {"action": "answer", "reason": "闲聊", "direct_text": "你好呀"}, ensure_ascii=False
    ))
    intent = IntentResult(intent=IntentType.CHAT, confidence=0.9)
    _run(decide_action("你好", intent, llm=llm, timeout=5.0))

    assert llm.kwargs, "应当调用 LLM"
    assert llm.kwargs[-1]["max_tokens"] == settings.LEAD_DECISION_MAX_TOKENS


def test_decide_action_merged_uses_configured_budget():
    from app.services.agents.lead.lead_decider import decide_action_merged

    llm = _CaptureLLM(json.dumps(
        {"intent": "chat", "action": "answer", "reason": "闲聊", "direct_text": "你好"},
        ensure_ascii=False,
    ))
    _run(decide_action_merged("你好", entry="chat", llm=llm, timeout=5.0))

    assert llm.kwargs, "应当调用 LLM"
    assert llm.kwargs[-1]["max_tokens"] == settings.LEAD_MERGED_MAX_TOKENS


def test_truncated_json_still_degrades_to_deterministic():
    """JSON 被截断（推理吃光预算的典型症状）→ 走确定性兜底，不抛异常。"""
    from app.services.agents.lead.lead_decider import decide_action

    llm = _CaptureLLM('{"action": "ans')      # 故意截断
    intent = IntentResult(intent=IntentType.CHAT, confidence=0.9)
    decision = _run(decide_action("你好", intent, llm=llm, timeout=5.0))

    assert decision is not None
    assert decision.degraded is True           # 降级标记可见（不是静默）
