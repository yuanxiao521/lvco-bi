"""D1.4 意图识别 · 脏 JSON 解析健壮性。

目标：LLM 经常不守规矩——包 markdown 围栏、前后带废话、混入多余字段。
验证 `extract_json_object` 能否把 JSON 抠出来，以及 `classify_intent` 的解析路径。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_intent_json_repair.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_intent import (  # noqa: E402
    classify_intent,
    extract_json_object,
)

DIRTY_OUTPUTS = [
    '```json\n{"intent": "data_qa", "confidence": 0.8}\n```',
    '好的，结果如下：\n{"intent": "analysis", "confidence": 0.9, "needs_plan": true}\n以上。',
    '{"intent": "canvas_edit", "confidence": 0.7, "extra_unknown": 1, "slots": {"block": "chart"}}',
    '这不是 JSON',
    '',
]


class StaticLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        return self._text


async def main() -> None:
    print("== extract_json_object 直测 ==")
    for raw in DIRTY_OUTPUTS:
        obj = extract_json_object(raw)
        print(f"  输入={raw!r}\n  → 抠出={obj}\n")

    print("== classify_intent 端到端（脏输出）==")
    for raw in DIRTY_OUTPUTS:
        degradation: list[str] = []
        result = await classify_intent("随便一句话", llm=StaticLLM(raw), degradation=degradation)
        print(
            f"  输出={raw[:34]!r:<36} → intent={result.intent.value:<11} "
            f"degraded={result.degraded}  {degradation}"
        )


if __name__ == "__main__":
    asyncio.run(main())
