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


