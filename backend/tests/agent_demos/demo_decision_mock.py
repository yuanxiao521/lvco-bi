"""D2.1 决策 · mock LLM 跑通 decide_action 四类分支。

目标：用一个假 LLM（`complete()` 返回预设决策 JSON）跑通 `decide_action` 全流程，
让 ANSWER / CALL_ANALYSIS / CANVAS_OP / ASK_USER 四个分支各走一次，
观察 Decision 的字段：action / tool_name / tool_args / direct_text / reason / degraded。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_decision_mock.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_decider import decide_action  # noqa: E402
from app.services.agents.lead.lead_intent import IntentResult, IntentType  # noqa: E402


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
        "title": "① ANSWER（直接回答）",
        "msg": "你好，你是谁",
        "intent": IntentResult(intent=IntentType.CHAT, confidence=0.95, needs_plan=False),
        "payload": {
            "action": "answer",
            "direct_text": "你好，我是主导 Agent，可以帮你做数据分析。",
            "reason": "纯闲聊，直接文本回答",
        },
    },
    {
        "title": "② CALL_ANALYSIS（委托分析编排器）",
        "msg": "帮我做一份完整的销售分析报告",
        "intent": IntentResult(intent=IntentType.ANALYSIS, confidence=0.92, needs_plan=True),
        "payload": {
            "action": "call_analysis",
            "tool_name": "run_analysis",
            "tool_args": {"goal": "完整的销售分析报告", "datasource_id": "ds_ecom"},
            "reason": "多步分析，交给确定性编排器",
        },
    },
    {
        "title": "③ CANVAS_OP（显式画布操作）",
        "msg": "在画布上新增一个图表块",
        "intent": IntentResult(intent=IntentType.CANVAS_EDIT, confidence=0.88, needs_plan=False),
        "payload": {
            "action": "canvas_op",
            "tool_name": "canvas_add_block",
            "tool_args": {"block_type": "chart", "position": "end"},
            "reason": "用户明确要求编辑画布",
        },
    },
    {
        "title": "④ ASK_USER（信息不足，反问）",
        "msg": "帮我分析一下",
        "intent": IntentResult(intent=IntentType.ANALYSIS, confidence=0.55, needs_plan=True),
        "payload": {
            "action": "ask_user",
            "direct_text": "请问要分析哪个数据源、什么时间段的数据？",
            "reason": "缺少数据源与时间范围，先澄清",
        },
    },
]


async def main() -> None:
    datasources = [{"id": "ds_ecom", "name": "电商订单", "type": "duckdb"}]
    for case in CASES:
        llm = MockLLM(case["payload"])
        degradation: list[str] = []
        decision = await decide_action(
            case["msg"],
            case["intent"],
            datasources=datasources,
            llm=llm,
            degradation=degradation,
        )
        print(case["title"])
        print(f"  用户：{case['msg']}")
        print(f"  → action={decision.action.value}")
        print(f"  → tool_name={decision.tool_name}  tool_args={decision.tool_args}")
        print(f"  → direct_text={decision.direct_text!r}")
        print(f"  → reason={decision.reason!r}  degraded={decision.degraded}  degradation={degradation}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
