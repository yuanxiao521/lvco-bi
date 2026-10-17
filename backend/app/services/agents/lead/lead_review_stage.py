# -*- coding: utf-8 -*-
"""P2 评审阶段：代码硬闸 + 条件裁量（**固定执行，不是工具**）。

任务书 §2.2.1 的三条理由（为什么不做成让模型按需调用的工具）：
  1. **做成工具就变成可选**——模型可以选择"不调评审"，达标判断就失去保障；
  2. **HARD 部分根本不需要 LLM**——执行失败 / 零落块 / 工具链失败，代码一算就知道；
  3. **评审的价值在"驱动下一轮"，不在"被查阅"**——它是每轮必过的工序，是阶段，不是能力。

形态：
    每轮派发子任务后 → 【评审阶段 · 固定执行】
      第一段 · 代码（零 LLM，硬闸）
          assess_subtask_detail → {text, hard[], soft[]}
      第二段 · Lead 裁量（**仅 SOFT 非空时**，一次调用，**由代码触发**，非模型自选）
          → {verdict: pass|fail, reason, guidance}
      安全边界：**HARD 非空 → verdict 强制 fail（Lead 不得翻案）**

输出 text 沿用 [子任务评估] 前缀写 turn_summaries——**零通道改动**。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.services.agents.lead.lead_tools import RunAnalysisResult, assess_subtask_detail

logger = logging.getLogger("lvco.lead.review_stage")

# 裁量输出的 token 上限：只要一个 JSON（verdict + 一句话 + 1-3 句 guidance）
_ADJUDICATE_MAX_TOKENS = 1024


@dataclass
class ReviewOutcome:
    """评审结论。"""

    text: str                                        # 写入 turn_summaries 的文本
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    verdict: str = "pass"                            # pass | fail
    reason: str = ""
    guidance: str = ""                               # fail 时给 Lead 的具体改进指导
    adjudicated: bool = False                        # 是否真的调了裁量（观测触发次数）

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


_FENCE = chr(96) * 3  # 代码块围栏（三个反引号），用 chr 避免转义歧义


def _extract_json(raw: str) -> dict:
    """从模型输出里抠出 JSON 对象（容忍代码块围栏与前后噪声）。"""
    text = (raw or "").strip()
    if text.startswith(_FENCE):
        text = text[len(_FENCE):].lstrip("json").strip()
        if text.endswith(_FENCE):
            text = text[: -len(_FENCE)].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def build_adjudicate_messages(
    *, user_goal: str, soft: list[str], execution_summary: str, system_text: str = ""
) -> list[dict]:
    """裁量输入：用户诉求 + 执行摘要 + SOFT 存疑项。"""
    from app.services.ai_prompts import LEAD_ADJUDICATE_SYSTEM

    lines = [
        "## 用户原始诉求",
        user_goal or "(未记录)",
        "",
        "## SOFT 存疑项（需要你判断是否构成真正的未达标）",
    ]
    lines += ([f"- {s}" for s in soft] or ["- (无)"])
    lines += ["", "## 子任务执行摘要", execution_summary or "(无)"]
    return [
        {"role": "system", "content": system_text or LEAD_ADJUDICATE_SYSTEM},
        {"role": "user", "content": "\n".join(lines)},
    ]


async def run_review_stage(
    result: RunAnalysisResult,
    *,
    entry: str,
    user_goal: str,
    llm,
    execution_summary: str = "",
    trace=None,
    degradation: list[str] | None = None,
    acceptance: dict | None = None,
) -> ReviewOutcome:
    """固定执行的评审阶段（见模块说明）。

    acceptance：Lead 在派发时**自己声明**的验收标准（P3）。声明缺失 → 跳过该检查。
    """
    detail = assess_subtask_detail(
        result, entry=entry, user_goal=user_goal, acceptance=acceptance
    )
    outcome = ReviewOutcome(
        text=detail.text, hard=list(detail.hard), soft=list(detail.soft),
        verdict="fail" if detail.hard else "pass",
        reason="；".join(detail.hard) if detail.hard else "",
    )
    # HARD 非空 → 硬闸直接 fail，**不调 LLM**（也绝不允许被翻案）
    if detail.hard:
        return outcome
    # 无 SOFT → 直接 pass，零新增调用（多数任务落这里，§4 P2 验收"多数任务零新增调用"）
    if not detail.soft:
        return outcome

    # 仅 SOFT 非空 → 一次裁量（由代码触发，非模型自选）
    outcome.adjudicated = True
    try:
        messages = build_adjudicate_messages(
            user_goal=user_goal, soft=detail.soft, execution_summary=execution_summary,
        )
        # enable_thinking=False：glm 系推理模型在思考被开启且 max_tokens 较小时，
        # 思考就把预算吃光、正文为空 → _extract_json 失败 → 裁量永远走降级（实测）。
        raw = await llm.complete(
            messages, max_tokens=_ADJUDICATE_MAX_TOKENS, enable_thinking=False,
        )
        obj = _extract_json(raw)
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in ("pass", "fail"):
            raise ValueError("裁量输出非法 verdict: " + repr(verdict))
        outcome.verdict = verdict
        outcome.reason = str(obj.get("reason") or "")[:300]
        outcome.guidance = str(obj.get("guidance") or "")[:600]
    except Exception:  # noqa: BLE001
        # §6 不变量 4：裁量失败 → **退回纯代码结论**（有 SOFT 无 HARD → 保守判 fail）
        logger.warning("[lead_review_stage] adjudicate_failed", exc_info=True)
        if degradation is not None:
            degradation.append("lead_adjudicate_failed")
        outcome.verdict = "fail"
        outcome.reason = outcome.reason or "裁量不可用，按存疑项保守判未达标"

    # 安全边界再确认：无论裁量说什么，HARD 非空都必须是 fail（本分支不该到达，双保险）
    if detail.hard:
        outcome.verdict = "fail"
    return outcome
