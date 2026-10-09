"""D1.2 意图识别 · mock LLM 跑通 classify_intent 全流程。

目标：用一个假 LLM（`complete()` 返回预设 JSON）跑通结构化解析路径，
观察 IntentResult 的五个字段：intent / confidence / slots / needs_plan / reason。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_intent_llm_mock.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_intent import classify_intent  # noqa: E402


class MockLLM:
    """最小 LLM 契约：只要实现 async complete(messages, **kw) -> str 即可。"""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.calls.append(messages)
        return json.dumps(self._payload, ensure_ascii=False)


CASES = [
    {
        "msg": "帮我做一份完整的销售分析报告",
        "payload": {
            "intent": "analysis",
            "confidence": 0.92,
            "slots": {"datasource": "电商订单", "time_range": "本月"},
            "needs_plan": True,
            "reason": "多步分析，需要编排器",
        },
    },
    {
        "msg": "本月总销售额是多少",
        "payload": {
            "intent": "data_qa",
            "confidence": 0.85,
            "slots": {"metric": "总销售额", "time_range": "本月"},
            "needs_plan": False,
            "reason": "单轮即可回答",
        },
    },
]


async def main() -> None:
    for case in CASES:
        llm = MockLLM(case["payload"])
        degradation: list[str] = []
        result = await classify_intent(case["msg"], llm=llm, degradation=degradation)
        print(f"用户：{case['msg']}")
        print(f"  → intent={result.intent.value}  confidence={result.confidence}")
        print(f"  → slots={result.slots}")
        print(f"  → needs_plan={result.needs_plan}  reason={result.reason!r}")
        print(f"  → degraded={result.degraded}  degradation={degradation}")
        print(f"  → 实际发给 LLM 的 user 内容：{llm.calls[0][-1]['content']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
