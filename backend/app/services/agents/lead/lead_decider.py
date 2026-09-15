"""决策：在意图已知的前提下，决定下一步动作（回答 / 调分析 / 画布操作 / 反问）。

设计要点（对齐 `lead-agent-upgrade-design.md` §4.2）：
- 决策与执行解耦：`CALL_ANALYSIS` 只产出一个 `tool_args`，真正执行在 `lead_tools.run_analysis`。
- 强约束 + 确定性兜底：异常 / 超时 / 空内容 / 非法枚举 → 按意图直接映射动作（`degraded=True`）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum

from app.services.agents.lead.lead_intent import (
    IntentResult,
    IntentType,
    extract_json_object,
    fallback_intent,
    parse_intent_fields,
)

logger = logging.getLogger("lvco.lead.decider")


class ActionType(str, Enum):
    """主导 Agent（Supervisor）可执行的动作枚举。

    注意：Supervisor 只决策"做什么任务"，不决策"用哪个执行器"——
    执行器由 entry（入口）确定性决定，复杂度分层复用旧双路径的路由分类器。
    """

    ANSWER = "answer"                # 直接文本回答
    CALL_ANALYSIS = "call_analysis"  # 派发分析/画布子任务（内核由 entry+复杂度决定）
    ASK_USER = "ask_user"            # 信息不足，反问
    STOP = "stop"                    # 主管收尾：主目标已完成，本轮对话结束


@dataclass
class Decision:
    """决策结果。"""

    action: ActionType
    tool_name: str | None = None
    tool_args: dict = field(default_factory=dict)
    direct_text: str | None = None
    reason: str = ""
    degraded: bool = False
    # 任务复杂度（Supervisor 决策顺带输出，供执行器选择，避免额外一次分类调用）：
    # "complex" → 对话页走 AgentOrchestrator；"simple" → 对话页走轻量 ReAct。
    # 画布页忽略此字段（入口决定内核）。兜底按意图映射。
    complexity: str = "complex"
    # Lead 指导意见（仅 call_analysis 有意义）：对上一轮子任务的评估 + 本轮要改进/补充什么。
    # 由 run_analysis 作为注入消息带给 Worker，让 Planner 带着评判重做而不是盲目重排。
    guidance: str = ""


def _guess_complexity(intent: IntentResult) -> str:
    """确定性兜底：按意图推断复杂度（不额外调用 LLM）。"""
    if intent.needs_plan or intent.intent == IntentType.ANALYSIS:
        return "complex"
    return "simple"


def _fallback_decision(user_msg: str, intent: IntentResult, reason: str) -> Decision:
    """确定性兜底：按意图直接映射动作（不做 LLM 猜测）。"""
    if intent.needs_plan or intent.intent == IntentType.ANALYSIS:
        return Decision(
            action=ActionType.CALL_ANALYSIS,
            tool_name="run_analysis",
            tool_args={"goal": user_msg},
            reason=reason,
            degraded=True,
            complexity="complex",
        )
    return Decision(
        action=ActionType.ANSWER,
        direct_text=None,
        reason=reason,
        degraded=True,
        complexity="simple",
    )


@dataclass
class MergedOutcome:
    """首轮合并调用产出：意图 + 决策 一次给出。"""

    intent: IntentResult
    decision: Decision


def _build_context_blocks(
    datasources: list[dict] | None,
    subtask_summaries: list[str] | None,
    canvas_layout: str = "",
) -> tuple[str, str, str, str]:
    """组装【可用数据源】【当前画布状态】【上一轮子任务评估】【已完成子任务摘要】四块（prompt 注入）。

    子任务摘要可能含报告全文（数百~千字），多轮累积会撑爆决策 prompt：
    只保留最近 2 条，且每条截断到 MAX_SUMMARY_CHARS，控制注入总量。
    canvas_layout 来自数据库落盘的画布布局（_load_canvas_snapshot），
    让 Supervisor 每轮决策都知道"画布上现已落成什么图表/文本、布局如何"。
    assessment_block 提取最近 1 条 [子任务评估]（规则评审产物），单独注入、不随摘要截断丢弃，
    决策据此判断"上轮是否达标"，决定 stop / 重做（带 guidance 改进意见）。
    """
    ds_lines = []
    for d in datasources or []:
        if not isinstance(d, dict):
            continue
        ds_lines.append(
            f"- id={d.get('id')} name={d.get('name')} type={d.get('type')}"
        )
    ds_block = "\n".join(ds_lines) or "（无）"

    canvas_block = (canvas_layout or "").strip()
    if not canvas_block:
        canvas_block = "（非画布入口或画布状态不可用）"

    # 最近 1 条子任务评估（规则评审，Short 前缀）
    assess_lines = [s for s in (subtask_summaries or []) if "[子任务评估]" in s]
    assessment_block = assess_lines[-1] if assess_lines else "（本轮无子任务评估）"

    # 最近 2 条 + 每条限长（保留工具链/规划/报告头部，丢弃过长的尾部）
    MAX_SUMMARY_CHARS = 1000
    recent = list(subtask_summaries or [])[-2:]
    summary_lines = [f"- {s[:MAX_SUMMARY_CHARS]}" for s in recent]
    summary_block = "\n".join(summary_lines) or "（本轮无已完成子任务）"
    return ds_block, canvas_block, assessment_block, summary_block


def _parse_decision(
    obj: dict,
    user_msg: str,
    intent: IntentResult,
    degradation: list[str] | None,
) -> Decision:
    """从 LLM 输出 dict 解析决策字段；结构非法时按意图确定性兜底（degraded=True）。"""
    raw_action = str(obj.get("action", "")).strip().lower()
    try:
        action = ActionType(raw_action)
    except ValueError:
        logger.warning(f"[lead_decider] invalid_action value={raw_action!r} fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_decision_fallback")
        return _fallback_decision(user_msg, intent, f"invalid_action:{raw_action}")

    tool_args = obj.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = {}
    tool_name = obj.get("tool_name")
    tool_name = str(tool_name) if tool_name else None
    direct_text = obj.get("direct_text")
    direct_text = str(direct_text) if direct_text else None
    reason = str(obj.get("reason") or "")
    guidance = str(obj.get("guidance") or "").strip()

    if action == ActionType.CALL_ANALYSIS:
        tool_name = tool_name or "run_analysis"
        if not tool_args.get("goal"):
            tool_args["goal"] = user_msg
        # 矛盾检测（few-shot 不灵时的代码层兜底）：LLM 输出 call_analysis 但 reason 自述
        # "需要澄清/缺少信息/先问"（尤其 canvas_edit 没给目标块）→ 强行纠正为 ask_user，
        # 避免"reason 说该问、action 却跑全量分析"（画布上会白白加一堆块）。
        _CLARIFY_MARKERS = ("需要澄清", "需要问", "需要补充", "需要先问", "缺少", "缺失",
                            "信息不足", "未指定", "不清楚", "请提供", "请告知")
        _cant_execute = any(m in reason for m in _CLARIFY_MARKERS)
        if _cant_execute:
            logger.warning(
                f"[lead_decider] contradiction action=call_analysis but reason suggests ask_user: "
                f"reason={reason[:60]} → corrected to ask_user"
            )
            if degradation is not None:
                degradation.append("lead_contradiction_fixed")
            return Decision(
                action=ActionType.ASK_USER,
                tool_name=None,
                tool_args={},
                direct_text=direct_text or "请补充必要信息后，我再帮你继续。",
                reason=reason or "信息不足，需要澄清",
                degraded=True,
                complexity="simple",
            )
    elif action in (ActionType.ANSWER, ActionType.ASK_USER) and not direct_text:
        # 该动作必须带文本，缺文本视为无效决策 → 兜底
        logger.warning(f"[lead_decider] missing_direct_text action={action.value} fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_decision_fallback")
        return _fallback_decision(user_msg, intent, "missing_direct_text")

    raw_complexity = str(obj.get("complexity") or "").strip().lower()
    if raw_complexity not in ("complex", "simple"):
        # 未输出 / 非法：按意图确定性兜底（不额外调用）
        raw_complexity = _guess_complexity(intent)
        logger.warning(f"[lead_decider] missing_complexity use_intent_guess={raw_complexity}")
    decision = Decision(
        action=action,
        tool_name=tool_name,
        tool_args=tool_args,
        direct_text=direct_text,
        reason=reason,
        degraded=False,
        complexity=raw_complexity,
        guidance=guidance,
    )
    logger.info(
        f"[lead_decider] action={action.value} tool={tool_name} "
        f"reason={reason[:60]}"
    )
    return decision


async def decide_action(
    user_msg: str,
    intent: IntentResult,
    *,
    history_summary: str = "",
    datasources: list[dict] | None = None,
    subtask_summaries: list[str] | None = None,
    canvas_layout: str = "",
    llm,
    timeout: float = 10.0,
    degradation: list[str] | None = None,
    prev_action: str | None = None,
    trace=None,
) -> Decision:
    """根据意图决定动作（Supervisor 版：可在多轮间传递子任务摘要）。

    Args:
        user_msg: 用户当前消息。
        intent: 上一步意图识别结果。
        history_summary: 会话长期记忆摘要。
        datasources: 可用数据源列表（供 LLM 引用真实 id，禁止编造）。
        subtask_summaries: 已完成子任务的产出摘要（Supervisor 据此避免重复执行）。
        canvas_layout: 画布布局快照（DB 落盘，画布入口注入，供感知已完成效果）。
        llm: LLM 客户端。
        timeout: LLM 调用超时（秒）。
        degradation: 可选外部列表，降级时写入 `lead_decision_fallback`。
        prev_action: 上一轮决策的动作名（answer/call_analysis/…），帮助收敛 stop。

    Returns:
        Decision；任何异常均不抛出，`degraded=True` 表示走了兜底。
    """
    from app.services.ai_prompts import LEAD_DECISION_SYSTEM

    ds_block, canvas_block, assessment_block, summary_block = _build_context_blocks(
        datasources, subtask_summaries, canvas_layout
    )

    user_content = (
        "【意图识别结果】\n"
        f"intent={intent.intent.value}\n"
        f"confidence={intent.confidence:.2f}\n"
        f"needs_plan={intent.needs_plan}\n"
        f"slots={json.dumps(intent.slots, ensure_ascii=False)}\n\n"
        "【上一轮动作】\n"
        f"{prev_action or '（本轮为首轮决策）'}\n\n"
        "【可用数据源】\n"
        f"{ds_block}\n\n"
        "【当前画布状态】\n"
        f"{canvas_block}\n\n"
        f"【上一轮子任务评估】\n{assessment_block}\n\n"
        "输出规则：若评估为未达标且你决定重新 call_analysis，必须同时在输出中给出 guidance 字段"
        "（1-3 句：点明上轮问题 + 本轮要如何改进/补充的指导意见），供下游 Worker 据此执行。\n\n"
        "【历史摘要】\n"
        f"{history_summary.strip() or '（无）'}\n\n"
        "【已完成子任务摘要】\n"
        f"{summary_block}\n\n"
        "【当前用户消息】\n"
        f"{user_msg}"
    )

    decision_messages = [
        {"role": "system", "content": LEAD_DECISION_SYSTEM},
        {"role": "user", "content": user_content},
    ]
    try:
        if trace is not None:
            from app.services.observability import observe_llm_call

            with observe_llm_call(trace, "lead_decision", messages=decision_messages) as span:
                result = await asyncio.wait_for(
                    llm.complete(
                        decision_messages,
                        response_format={"type": "json_object"},
                        enable_thinking=False,
                        max_tokens=400,
                        return_usage=True,
                    ),
                    timeout=timeout,
                )
                content, usage_meta = result if isinstance(result, tuple) else (result, None)
                if usage_meta:
                    span.update(usage=usage_meta)
        else:
            content = await asyncio.wait_for(
                llm.complete(
                    decision_messages,
                    response_format={"type": "json_object"},
                    enable_thinking=False,
                    max_tokens=400,
                ),
                timeout=timeout,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_decider] llm_error error={e} fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_decision_fallback")
        return _fallback_decision(user_msg, intent, f"llm_error:{e}")

    obj = extract_json_object(content or "")
    if not obj:
        logger.warning("[lead_decider] empty_or_unparsable fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_decision_fallback")
        return _fallback_decision(user_msg, intent, "unparsable")

    return _parse_decision(obj, user_msg, intent, degradation)


# ── 首轮合并调用：意图 + 决策 一次 LLM 请求 ────────────────────────────────

# 附在 LEAD_INTENT_SYSTEM + LEAD_DECISION_SYSTEM 之后，说明合并调用的职责与输出字段
_MERGED_SYSTEM_TAIL = (
    "════════ 首轮合并调用说明 ════════\n"
    "本次调用同时负责【意图识别】与【决策】两项任务（仅首轮如此，后续轮次只做决策）。\n"
    "你必须先按上方意图规则识别当前用户消息，得到 intent，再按上方决策规则决定下一步 action。\n"
    "输出 JSON 必须同时包含两组字段：\n"
    "- 意图字段：intent / confidence / needs_plan / slots\n"
    "- 决策字段：action / tool_name / tool_args / direct_text / reason / complexity\n"
    "示例：{\"intent\": \"analysis\", \"confidence\": 0.95, \"needs_plan\": true, \"slots\": {\"metric\": \"销售额\"}, "
    "\"action\": \"call_analysis\", \"tool_name\": \"run_analysis\", \"tool_args\": {\"goal\": \"分析2024年销售额趋势\"}, "
    "\"direct_text\": null, \"reason\": \"多步分析任务\", \"complexity\": \"complex\"}\n"
    "更多动作示例（按场景选一个）：\n"
    "- 需要用户澄清/补信息时（必须 action=ask_user，绝不能 reason 说该问、action 却填 call_analysis）："
    "{\"intent\": \"canvas_edit\", \"confidence\": 0.9, \"needs_plan\": false, \"slots\": {}, "
    "\"action\": \"ask_user\", \"tool_name\": null, \"tool_args\": {}, \"direct_text\": \"请告诉我要修改哪个块\", "
    "\"reason\": \"缺少目标块信息，需要澄清\", \"complexity\": \"simple\"}\n"
    "- 简单问答可直接给文本（action=answer）：{\"intent\": \"chat\", \"confidence\": 0.9, \"needs_plan\": false, \"slots\": {}, "
    "\"action\": \"answer\", \"tool_name\": null, \"tool_args\": {}, \"direct_text\": \"好的，请问需要分析哪部分数据？\", "
    "\"reason\": \"闲聊直接回答\", \"complexity\": \"simple\"}\n"
    "- 主目标已完成、无新任务（action=stop）：{\"intent\": \"analysis\", \"confidence\": 0.9, \"needs_plan\": false, \"slots\": {}, "
    "\"action\": \"stop\", \"tool_name\": null, \"tool_args\": {}, \"direct_text\": null, "
    "\"reason\": \"子任务已完成，主目标达成\", \"complexity\": \"simple\"}\n"
    "【自洽硬规则】reason 与 action 必须一致：若判断需要先问用户（缺信息/缺目标块），action 必须填 ask_user。"
    "【重新派发规则】若【上一轮子任务评估】为'未达标'且你决定再次 call_analysis，"
    "必须附加 guidance 字段（1-3 句：点明上轮问题 + 本轮如何改进），并在 tool_args.goal 中明确本轮目标；"
    "若评估已达标且无新任务，必须 action=stop，不要重复执行。"
)


async def decide_action_merged(
    user_msg: str,
    *,
    history_summary: str = "",
    datasources: list[dict] | None = None,
    subtask_summaries: list[str] | None = None,
    canvas_layout: str = "",
    llm,
    timeout: float = 10.0,
    degradation: list[str] | None = None,
    trace=None,
) -> MergedOutcome:
    """首轮合并调用：一次 LLM 请求同时产出【意图】+【决策】。

    失败降级成对处理：意图走规则兜底（fallback_intent），决策走意图映射兜底
    （_fallback_decision），保证 call_analysis / answer 至少有一个确定性输出。
    意图字段非法时视为整次调用降级（决策规则依赖意图，避免基于错误意图的决策）。

    Returns:
        MergedOutcome；任何异常均不抛出，`intent.degraded` / `decision.degraded` 标记兜底。
    """
    from app.services.ai_prompts import LEAD_DECISION_SYSTEM, LEAD_INTENT_SYSTEM

    # 合并 system：意图规则 + 决策规则（去掉决策段顶部"意图识别已完成"的旧表述，改为本调用自行识别）
    decision_part = LEAD_DECISION_SYSTEM.replace(
        "意图识别已完成，你的职责是决定",
        "你的职责是决定",
    )
    system = f"{LEAD_INTENT_SYSTEM}\n\n{decision_part}\n\n{_MERGED_SYSTEM_TAIL}"
    ds_block, canvas_block, assessment_block, summary_block = _build_context_blocks(
        datasources, subtask_summaries, canvas_layout
    )

    user_content = (
        "【本轮为合并调用】请先识别意图，再决定动作，一次输出全部字段。\n\n"
        "【历史摘要（含最近对话）】\n"
        f"{history_summary.strip() or '（无）'}\n\n"
        "【可用数据源】\n"
        f"{ds_block}\n\n"
        "【当前画布状态】\n"
        f"{canvas_block}\n\n"
        f"【上一轮子任务评估】\n{assessment_block}\n\n"
        "输出规则：若评估为未达标且你决定重新 call_analysis，必须同时在输出中给出 guidance 字段"
        "（1-3 句：点明上轮问题 + 本轮要如何改进/补充的指导意见），供下游 Worker 据此执行。\n\n"
        "【已完成子任务摘要】\n"
        f"{summary_block}\n\n"
        "【当前用户消息】\n"
        f"{user_msg}"
    )

    merged_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    try:
        if trace is not None:
            from app.services.observability import observe_llm_call

            with observe_llm_call(trace, "lead_intent_decision", messages=merged_messages) as span:
                result = await asyncio.wait_for(
                    llm.complete(
                        merged_messages,
                        response_format={"type": "json_object"},
                        enable_thinking=False,
                        max_tokens=600,
                        return_usage=True,
                    ),
                    timeout=timeout,
                )
                content, usage_meta = result if isinstance(result, tuple) else (result, None)
                if usage_meta:
                    span.update(usage=usage_meta)
        else:
            content = await asyncio.wait_for(
                llm.complete(
                    merged_messages,
                    response_format={"type": "json_object"},
                    enable_thinking=False,
                    max_tokens=600,
                ),
                timeout=timeout,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_decider] merged_llm_error error={e} fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_intent_fallback")
            degradation.append("lead_decision_fallback")
        intent = fallback_intent(user_msg, history_summary, f"llm_error:{e}")
        return MergedOutcome(
            intent=intent,
            decision=_fallback_decision(user_msg, intent, f"llm_error:{e}"),
        )

    obj = extract_json_object(content or "")
    if not obj:
        logger.warning("[lead_decider] merged_empty_or_unparsable fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_intent_fallback")
            degradation.append("lead_decision_fallback")
        intent = fallback_intent(user_msg, history_summary, "unparsable")
        return MergedOutcome(
            intent=intent,
            decision=_fallback_decision(user_msg, intent, "unparsable"),
        )

    intent, reason = parse_intent_fields(obj, user_msg, history_summary)
    if intent is None:
        logger.warning(f"[lead_decider] merged_invalid_intent reason={reason} fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_intent_fallback")
        intent = fallback_intent(user_msg, history_summary, reason)
        return MergedOutcome(
            intent=intent,
            decision=_fallback_decision(user_msg, intent, reason),
        )

    decision = _parse_decision(obj, user_msg, intent, degradation)
    logger.info(
        f"[lead_decider] merged intent={intent.intent.value} confidence={intent.confidence:.2f} "
        f"action={decision.action.value} complexity={decision.complexity}"
    )
    return MergedOutcome(intent=intent, decision=decision)
