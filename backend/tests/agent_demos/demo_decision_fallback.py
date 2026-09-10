"""D2.2 决策 · 注入 LLM 失败，验证确定性兜底。

目标：演示"决策 LLM 挂了怎么办"——`decide_action` 绝不抛异常，
而是按 `intent.needs_plan / intent == ANALYSIS` 直接映射动作，并置 `degraded=True`。

故障注入：
    1. RaiseLLM   抛异常
    2. EmptyLLM   返回空串
    3. BadAction  返回非法枚举 action
    4. NoText     返回 answer 但缺 direct_text

运行（在 backend 目录下）：
    python tests/agent_demos/demo_decision_fallback.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_decider import decide_action  # noqa: E402
from app.services.agents.lead.lead_intent import IntentResult, IntentType  # noqa: E402


class RaiseLLM:
    async def complete(self, messages, **kw):  # noqa: ANN001
        raise RuntimeError("upstream 502")


class EmptyLLM:
    async def complete(self, messages, **kw):  # noqa: ANN001
        return ""


class BadActionLLM:
    async def complete(self, messages, **kw):  # noqa: ANN001
        return '{"action": "teleport", "reason": "乱写"}'


class NoTextLLM:
    async def complete(self, messages, **kw):  # noqa: ANN001
        return '{"action": "answer", "reason": "忘了带文本"}'


FAILURES = [
    ("抛异常", RaiseLLM()),
    ("返回空", EmptyLLM()),
    ("非法枚举", BadActionLLM()),
    ("缺文本", NoTextLLM()),
]

INTENTS = [
    ("ANALYSIS + needs_plan=True", IntentResult(intent=IntentType.ANALYSIS, needs_plan=True)),
    ("CHAT + needs_plan=False", IntentResult(intent=IntentType.CHAT, needs_plan=False)),
]


async def run_case(title: str, llm, intent: IntentResult) -> None:
    degradation: list[str] = []
    decision = await decide_action(
        "帮我做一份销售分析报告",
        intent,
        llm=llm,
        degradation=degradation,
    )
    print(f"  [{title}] → action={decision.action.value} "
          f"tool={decision.tool_name} degraded={decision.degraded} "
          f"reason={decision.reason!r} degradation={degradation}")


async def main() -> None:
    for name, llm in FAILURES:
        print(f"故障注入：{name}")
        for label, intent in INTENTS:
            await run_case(label, llm, intent)
        print()

    print("结论：")
    print("  - needs_plan=True / ANALYSIS → 兜底为 CALL_ANALYSIS（保证分析不中断）")
    print("  - 其余 → 兜底为 ANSWER（交给上层用文本回答）")
    print("  - 所有故障都置 degraded=True 且不抛异常")


if __name__ == "__main__":
    asyncio.run(main())
