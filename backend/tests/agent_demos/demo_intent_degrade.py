"""D1.3 意图识别 · 三种故障注入，验证确定性兜底。

目标：演示"分类器挂了怎么办"——LLM 抛异常 / 返回空 / 超时，
是否都落到规则意图，并且 `degraded=True` + 降级原因码 `lead_intent_fallback`。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_intent_degrade.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_intent import classify_intent  # noqa: E402


class RaiseLLM:
    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        raise RuntimeError("boom")


class EmptyLLM:
    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        return ""


class SlowLLM:
    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        await asyncio.sleep(1.0)
        return "{}"


async def run_case(title: str, llm, msg: str) -> None:
    degradation: list[str] = []
    result = await classify_intent(msg, llm=llm, timeout=0.05, degradation=degradation)
    print(f"[{title}] 用户：{msg}")
    print(f"  → intent={result.intent.value}  confidence={result.confidence}")
    print(f"  → degraded={result.degraded}  reason={result.reason!r}")
    print(f"  → degradation={degradation}")
    print()


async def main() -> None:
    await run_case("异常", RaiseLLM(), "帮我做一份销售分析报告")
    await run_case("空内容", EmptyLLM(), "本月总销售额是多少")
    await run_case("超时", SlowLLM(), "在画布上新增一个图表块")
    print("观察点：三条都 degraded=True，且意图来自关键词规则（analysis/data_qa/canvas_edit）。")


if __name__ == "__main__":
    asyncio.run(main())
