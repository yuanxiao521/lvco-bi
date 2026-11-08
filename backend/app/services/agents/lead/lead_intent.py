# -*- coding: utf-8 -*-
"""意图类型（**只剩类型定义**）。

历史：本模块原先是一个完整的"意图识别工位"——LLM 分类 + 槽位抽取（`classify_intent`）、
规则兜底（`_rule_intent`）、JSON 修复（`extract_json_object`）。那是 legacy 四工位的
第一站，已随 legacy 一起删除（见 docs/SUPER_OPTIMIZATION_MVP.md）。

为什么还留着：`lead_intent_signal`（统一循环的"理解层标签"）需要这两个类型，且
`INTENT_VALUES` 的枚举与 SSE `intent` 事件的取值绑定——删了会让前端标签失去词表。

注意：统一循环**不用**这个枚举做路由（路由由工具选择 + effective_complexity 决定），
它只是标签：展示给用户 + 监控"理解漂移"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class IntentType(str, Enum):
    """用户意图枚举（展示用标签，不参与路由）。"""

    CHAT = "chat"                # 闲聊 / 概念问答
    DATA_QA = "data_qa"          # 问数据（单轮即可答）
    CANVAS_EDIT = "canvas_edit"  # 改画布（落块 / 删块 / 调布局）
    ANALYSIS = "analysis"        # 多步分析（需要编排器）
    FOLLOWUP = "followup"        # 对上文的追问 / 修正


@dataclass
class IntentResult:
    """意图识别结果（保留给 lead_intent_signal 的派生逻辑使用）。"""

    intent: IntentType
    confidence: float = 0.0
    slots: dict = field(default_factory=dict)
    needs_plan: bool = False
    reason: str = ""
    degraded: bool = False
