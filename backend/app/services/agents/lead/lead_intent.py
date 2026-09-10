"""意图识别：把用户一句话归类到五类意图之一，并抽取关键槽位。

设计要点（对齐 `lead-agent-upgrade-design.md` §4.1）：
- LLM 只做分类 + 槽位抽取，不执行工具、不回答用户。
- 强约束 + 确定性兜底：异常 / 超时 / 空内容 / 非法枚举 → 走规则意图（`degraded=True`）。
- 取代旧 `_classify_task_complexity` 的布尔位；旧函数保留供兜底路径使用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("lvco.lead.intent")


class IntentType(str, Enum):
    """用户意图枚举。"""

    CHAT = "chat"                # 闲聊 / 概念问答
    DATA_QA = "data_qa"          # 问数据（单轮即可答）
    CANVAS_EDIT = "canvas_edit"  # 改画布（落块 / 删块 / 调布局）
    ANALYSIS = "analysis"        # 多步分析（需要编排器）
    FOLLOWUP = "followup"        # 对上文的追问 / 修正


@dataclass
class IntentResult:
    """意图识别结果。"""

    intent: IntentType
    confidence: float = 0.0
    slots: dict = field(default_factory=dict)
    needs_plan: bool = False
    reason: str = ""
    degraded: bool = False


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict | None:
    """从 LLM 输出中提取第一个 JSON 对象（容忍代码块围栏 / 前后废话）。"""
    if not text:
        return None
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    for m in _FENCE_RE.finditer(stripped):
        candidates.append(m.group(1).strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


_CANVAS_KW = (
    "画布", "新增块", "删除块", "加一个块", "添加块", "布局", "换个图", "换一个图",
    "文本块", "图表块", "删掉", "移除块", "改标题", "调整顺序",
)
_ANALYSIS_KW = (
    "分析", "报告", "对比", "同比", "环比", "趋势", "归因", "看板", "完整",
    "多维度", "拆解", "诊断", "复盘", "洞察",
)
_DATA_KW = (
    "多少", "是多少", "查询", "统计", "列出", "有哪些", "排名", "top", "销售额",
    "订单", "数量", "平均值", "总额", "占比", "增长率", "分布",
)
_FOLLOWUP_KW = ("那", "再", "继续", "接着", "为什么", "还有", "换成", "改成", "然后")


def _rule_intent(user_msg: str, history_summary: str = "") -> tuple[IntentType, float]:
    """确定性规则意图：LLM 不可用时的兜底，返回 (意图, 置信度)。"""
    t = (user_msg or "").strip()
    if not t:
        return IntentType.CHAT, 0.0
    if any(k in t for k in _CANVAS_KW):
        return IntentType.CANVAS_EDIT, 0.5
    if any(k in t for k in _ANALYSIS_KW):
        return IntentType.ANALYSIS, 0.5
    if history_summary and any(k in t for k in _FOLLOWUP_KW):
        return IntentType.FOLLOWUP, 0.4
    low = t.lower()
    if any(k in t or k in low for k in _DATA_KW):
        return IntentType.DATA_QA, 0.4
    return IntentType.CHAT, 0.2


def _degraded_result(user_msg: str, history_summary: str, reason: str) -> IntentResult:
    intent, conf = _rule_intent(user_msg, history_summary)
    return IntentResult(
        intent=intent,
        confidence=conf,
        slots={},
        needs_plan=intent == IntentType.ANALYSIS,
        reason=reason,
        degraded=True,
    )


async def classify_intent(
    user_msg: str,
    *,
    history_summary: str = "",
    llm,
    timeout: float = 8.0,
    degradation: list[str] | None = None,
) -> IntentResult:
    """识别用户意图。

    Args:
        user_msg: 用户当前消息。
        history_summary: 会话长期记忆摘要（帮助判定 followup）。
        llm: LLM 客户端（需实现 `complete(messages, ...)`）。
        timeout: LLM 调用超时（秒），超时降级为规则意图。
        degradation: 可选外部列表，降级时写入原因码 `lead_intent_fallback`。

    Returns:
        IntentResult；任何异常均不抛出，`degraded=True` 表示走了兜底。
    """
    from app.services.ai_prompts import LEAD_INTENT_SYSTEM

    user_content = user_msg
    if history_summary and history_summary.strip():
        user_content = f"【历史摘要】{history_summary.strip()}\n\n【当前用户消息】{user_msg}"

    try:
        content = await asyncio.wait_for(
            llm.complete(
                [
                    {"role": "system", "content": LEAD_INTENT_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                enable_thinking=False,
                max_tokens=300,
            ),
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_intent] llm_error error={e} fallback=rule")
        if degradation is not None:
            degradation.append("lead_intent_fallback")
        return _degraded_result(user_msg, history_summary, f"llm_error:{e}")

    obj = extract_json_object(content or "")
    if not obj:
        logger.warning("[lead_intent] empty_or_unparsable fallback=rule")
        if degradation is not None:
            degradation.append("lead_intent_fallback")
        return _degraded_result(user_msg, history_summary, "unparsable")

    raw_intent = str(obj.get("intent", "")).strip().lower()
    try:
        intent = IntentType(raw_intent)
    except ValueError:
        logger.warning(f"[lead_intent] invalid_intent value={raw_intent!r} fallback=rule")
        if degradation is not None:
            degradation.append("lead_intent_fallback")
        return _degraded_result(user_msg, history_summary, f"invalid_intent:{raw_intent}")

    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    slots = obj.get("slots")
    if not isinstance(slots, dict):
        slots = {}

    needs_plan = bool(obj.get("needs_plan")) or intent == IntentType.ANALYSIS
    reason = str(obj.get("reason") or "")

    result = IntentResult(
        intent=intent,
        confidence=confidence,
        slots=slots,
        needs_plan=needs_plan,
        reason=reason,
        degraded=False,
    )
    logger.info(
        f"[lead_intent] intent={intent.value} confidence={confidence:.2f} "
        f"needs_plan={needs_plan} slots={list(slots.keys())}"
    )
    return result
