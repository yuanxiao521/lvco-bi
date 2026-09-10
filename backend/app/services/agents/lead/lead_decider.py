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

from app.services.agents.lead.lead_intent import IntentResult, IntentType, extract_json_object

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


async def decide_action(
    user_msg: str,
    intent: IntentResult,
    *,
    history_summary: str = "",
    datasources: list[dict] | None = None,
    subtask_summaries: list[str] | None = None,
    llm,
    timeout: float = 10.0,
    degradation: list[str] | None = None,
) -> Decision:
    """根据意图决定动作（Supervisor 版：可在多轮间传递子任务摘要）。

    Args:
        user_msg: 用户当前消息。
        intent: 上一步意图识别结果。
        history_summary: 会话长期记忆摘要。
        datasources: 可用数据源列表（供 LLM 引用真实 id，禁止编造）。
        subtask_summaries: 已完成子任务的产出摘要（Supervisor 据此避免重复执行）。
        llm: LLM 客户端。
        timeout: LLM 调用超时（秒）。
        degradation: 可选外部列表，降级时写入 `lead_decision_fallback`。

    Returns:
        Decision；任何异常均不抛出，`degraded=True` 表示走了兜底。
    """
    from app.services.ai_prompts import LEAD_DECISION_SYSTEM

    ds_lines = []
    for d in datasources or []:
        if not isinstance(d, dict):
            continue
        ds_lines.append(
            f"- id={d.get('id')} name={d.get('name')} type={d.get('type')}"
        )
    ds_block = "\n".join(ds_lines) or "（无）"

    summary_lines = [f"- {s}" for s in (subtask_summaries or [])]
    summary_block = "\n".join(summary_lines) or "（本轮无已完成子任务）"

    user_content = (
        "【意图识别结果】\n"
        f"intent={intent.intent.value}\n"
        f"confidence={intent.confidence:.2f}\n"
        f"needs_plan={intent.needs_plan}\n"
        f"slots={json.dumps(intent.slots, ensure_ascii=False)}\n\n"
        "【可用数据源】\n"
        f"{ds_block}\n\n"
        "【历史摘要】\n"
        f"{history_summary.strip() or '（无）'}\n\n"
        "【已完成子任务摘要】\n"
        f"{summary_block}\n\n"
        "【当前用户消息】\n"
        f"{user_msg}"
    )

    try:
        content = await asyncio.wait_for(
            llm.complete(
                [
                    {"role": "system", "content": LEAD_DECISION_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
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

    if action == ActionType.CALL_ANALYSIS:
        tool_name = tool_name or "run_analysis"
        if not tool_args.get("goal"):
            tool_args["goal"] = user_msg
    elif action in (ActionType.ANSWER, ActionType.ASK_USER) and not direct_text:
        # 该动作必须带文本，缺文本视为无效决策 → 兜底
        logger.warning(f"[lead_decider] missing_direct_text action={action.value} fallback=deterministic")
        if degradation is not None:
            degradation.append("lead_decision_fallback")
        return _fallback_decision(user_msg, intent, "missing_direct_text")

    reason = str(obj.get("reason") or "")
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
    )
    logger.info(
        f"[lead_decider] action={action.value} tool={tool_name} "
        f"reason={reason[:60]}"
    )
    return decision
