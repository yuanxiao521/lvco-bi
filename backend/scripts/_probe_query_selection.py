"""Step 3 手动回归：真实 LLM 下验证查询工具选型（临时脚本，用后即删）。

不依赖数据库：注入模拟数据源列表，驱动真实 PlannerAgent（新版 orchestrator prompt/示例）
与完整 AgentOrchestrator（真实工具描述），观察 planner 步骤选型 + executor 真实 tool_call。
预期（Step 3 边界）：
- 对比/占比/排名/过滤类 → query_engine
- 时间趋势(date_trunc)/明细/窗口/CTE → query_datasource
"""
from __future__ import annotations

import asyncio
import json

from app.services.agents.planner_agent import PlannerAgent
from app.services.llm_client import LLMClient

FAKE_DS = [{
    "id": "demo-orders",
    "name": "电商订单",
    "description": "电商订单明细数据",
    "type": "csv",
    "table_ref": '"u_demo_orders"."data"',
    "fields": [
        {"name": "order_id", "data_type": "VARCHAR", "sample": ["A1001", "A1002"]},
        {"name": "order_date", "data_type": "DATE", "sample": ["2026-07-01", "2026-07-02"]},
        {"name": "category", "data_type": "VARCHAR", "sample": ["手机", "家电", "服装"]},
        {"name": "region", "data_type": "VARCHAR", "sample": ["华东", "华北", "华南"]},
        {"name": "amount", "data_type": "DOUBLE", "sample": [1999.0, 89.5]},
        {"name": "quantity", "data_type": "INTEGER", "sample": [2, 1]},
    ],
}]

QUESTIONS = [
    ("对比各品类的销售额", "expect query_engine"),
    ("计算华东和华北地区本月的销售额对比", "expect query_engine"),
    ("各产品类别的销售数量排名", "expect query_engine"),
    ("最近30天每天的销售额趋势", "expect query_datasource(时间趋势/date_trunc)"),
    ("订单金额分布情况", "expect query_datasource(明细数据)"),
]


async def probe_planner(q_filter: str | None = None) -> None:
    llm = LLMClient()
    planner = PlannerAgent(llm)
    print("\n===== Planner 选型（真实 LLM + 新版 prompt/示例）=====")
    for q, expect in QUESTIONS:
        if q_filter and q_filter not in q:
            continue
        result = await planner.execute(
            user_msg=q, history=[], available_datasources=FAKE_DS,
        )
        steps = result.data.get("steps", []) if result.success else []
        tools = [f"#{s['step_id']}={s.get('tool') or '无'}" for s in steps]
        print(f"\n[Q] {q}\n    {expect}\n    PLAN: {', '.join(tools)}")
        for s in steps:
            print(f"        {s.get('tool')}: {s.get('goal', '')[:70]}")


async def probe_orchestrator_e2e() -> None:
    from app.services.agents.agent_orchestrator import AgentOrchestrator

    llm = LLMClient()
    orch = AgentOrchestrator(llm, None)
    print("\n===== 完整编排执行（观察 executor 真实 tool_call）=====")
    called: list[str] = []
    async for ev in orch.execute_task(
        user_msg="对比各品类的销售额占比",
        history=[],
        user_id="demo-user",
        available_datasources=FAKE_DS,
    ):
        if ev.get("type") == "tool_call":
            called.append(ev.get("name", "?"))
            print(f"  [tool_call] {ev.get('name')} args={json.dumps(ev.get('args', {}), ensure_ascii=False)[:120]}")
        elif ev.get("type") == "plan":
            plan_tools = [s.get("tool") for s in ev.get("plan", {}).get("steps", [])]
            print(f"  [plan] steps_tools={plan_tools}")
        elif ev.get("type") in ("error", "done"):
            print(f"  [{ev.get('type')}]")
    print(f"  实际工具调用序列: {called}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "planner-only":
        asyncio.run(probe_planner(sys.argv[2] if len(sys.argv) > 2 else None))
    else:
        asyncio.run(probe_planner())
        asyncio.run(probe_orchestrator_e2e())