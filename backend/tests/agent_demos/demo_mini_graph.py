"""D2.3 决策 · 用 graph.py 手搓 3 节点图 + 条件边。

目标：理解本项目自研的轻量图引擎（LangGraph 模式，零依赖）：
- 顺序边 add_edge
- 条件边 add_conditional_edges（router 返回 route 名，按 mapping 跳转）
- 共享 state、`__steps__` 步骤日志、`_MAX_STEPS` 防死循环

图结构：
    plan ──> exec ──(条件)──> report   (need_report=True)
                     └────────> end    (need_report=False)

运行（在 backend 目录下）：
    python tests/agent_demos/demo_mini_graph.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.graph import _MAX_STEPS, Graph  # noqa: E402


async def node_plan(state: dict, **shared) -> dict:
    print("    [plan] 拆解目标：", state.get("goal"))
    return {"steps": ["取数", "聚合", "出图"]}


async def node_exec(state: dict, **shared) -> dict:
    print(f"    [exec] 执行 {len(state.get('steps', []))} 个步骤")
    return {"rows": 128, "chart_ok": state.get("want_chart", False)}


async def node_report(state: dict, **shared) -> dict:
    print(f"    [report] 生成报告：{state.get('rows')} 行数据")
    return {"report": f"报告({state.get('rows')}行)"}


async def node_end(state: dict, **shared) -> dict:
    print("    [end] 无图需求，直接结束")
    return {}


async def router_after_exec(state: dict, **shared) -> str:
    return "with_chart" if state.get("chart_ok") else "no_chart"


def build_graph() -> Graph:
    g = Graph("mini")
    g.add_node("plan", node_plan)
    g.add_node("exec", node_exec)
    g.add_node("report", node_report)
    g.add_node("end", node_end)
    g.set_entry_point("plan")
    g.add_edge("plan", "exec")
    g.add_conditional_edges(
        "exec",
        router_after_exec,
        {"with_chart": "report", "no_chart": "end"},
    )
    g.set_finish_point("report", "end")
    return g


async def demo_loop_guard() -> None:
    print("② 防死循环：自环节点（每次加 1），观察 guard 触发")
    g = Graph("loop")

    async def tick(state: dict, **shared) -> dict:
        return {"n": state.get("n", 0) + 1}

    g.add_node("tick", tick)
    g.set_entry_point("tick")
    g.add_edge("tick", "tick")
    state = await g.invoke({})
    print(f"    n={state.get('n')}  __error__={state.get('__error__')}")
    print(f"    步骤日志长度={len(state['__steps__'])}（应等于 _MAX_STEPS={_MAX_STEPS}）")


async def main() -> None:
    print("① 三节点图 + 条件边")
    g = build_graph()

    print("  [场景 A] want_chart=True → 走 report")
    state_a = await g.invoke({"goal": "本月销售分析", "want_chart": True})
    print(f"    __steps__ 路由序列：{[s.get('route') for s in state_a['__steps__']]}")
    print(f"    最终 report={state_a.get('report')}")
    print()

    print("  [场景 B] want_chart=False → 走 end")
    state_b = await g.invoke({"goal": "只要一个数字", "want_chart": False})
    print(f"    __steps__ 路由序列：{[s.get('route') for s in state_b['__steps__']]}")
    print(f"    最终 report={state_b.get('report')}")
    print()

    await demo_loop_guard()


if __name__ == "__main__":
    asyncio.run(main())
