"""D3.2 感知 · 把假事件流喂给 perceive_stream。

目标：演示感知层如何"旁路"编排器的事件流，翻译成连续的 StepProgress。
这里用 async generator 手造一段事件流，不需要真跑编排器。

关键契约：编排器把「步骤结果」事件的 name 设为 `goal[:20]`，
`perceive_stream` 用这一点识别"某步骤结束"并推进到下一步。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_progress_stream.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_perception import (  # noqa: E402
    perceive_stream,
    render_progress_text,
)

PLAN = {
    "task_summary": "本月销售分析",
    "steps": [
        {"step_id": "1", "goal": "查询本月销售总额"},
        {"step_id": "2", "goal": "按地区聚合销售数据"},
        {"step_id": "3", "goal": "生成柱状图"},
    ],
}

EVENTS = [
    {"type": "plan", "plan": PLAN},
    {"type": "status", "message": "正在分析需求并生成执行计划..."},
    {"type": "tool_call", "name": "query_sql"},
    {"type": "tool_result", "name": "查询本月销售总额", "result": '{"rows": 1}'},
    {"type": "tool_call", "name": "query_sql"},
    {"type": "tool_result", "name": "按地区聚合销售数据", "result": '{"error": "字段 gender 不存在"}'},
    {"type": "chart", "chart_type": "bar"},
    {"type": "tool_result", "name": "生成柱状图", "result": '{"ok": true}'},
]


async def fake_stream():
    for ev in EVENTS:
        yield ev


async def main() -> None:
    print("原始事件流 → StepProgress 汇报：\n")
    async for p in perceive_stream(fake_stream()):
        print(f"  {render_progress_text(p)}")

    print("\n观察点：")
    print("  - plan 到达后 total 从 0 变成 3，index 从 1 开始")
    print("  - tool_result 的 name 命中 goal[:20] → 判为步骤结束并推进")
    print("  - result 含 error → status=fail（感知层能识别失败）")
    print("  - 原始事件照样透传前端，感知只是'多看一眼'")


if __name__ == "__main__":
    asyncio.run(main())
