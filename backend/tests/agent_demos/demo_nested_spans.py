"""D5.2 监控 · 嵌套 span 的层级如何验证（本地模式）。

真实路径：LeadAgent 开启一次 trace("lead_agent_turn")（lead_agent.py），
把 trace 透传给后续各阶段——意图识别、决策各是一次 LLM 调用；
run_analysis 是一个 chain 型 span（lead_tools.py `trace.span("run_analysis", span_type="chain")`），
其内部再产生工具调用 step 级 span。

本地模式下 trace.children 是「扁平列表」（Langfuse 启用后由 SDK 真实嵌套）。
因此本 demo 演示：
1) 按真实调用顺序向同一 trace 追加各层 span；
2) 用一个「父 → 子」记录表，把扁平列表重建回层级树；
3) 打印树并核对每一层 span 数量 / 类型 / 耗时。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_nested_spans.py
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.observability import (  # noqa: E402
    SpanRecord,
    get_observer,
    observe_llm_call,
    observe_tool_call,
)


def rebuild_tree(children: list[SpanRecord], parent_map: dict[int, int | None]) -> list[dict]:
    """把扁平 children + parent_map 重建成树。返回根节点列表（每个节点含 name/type/latency/kids）。"""
    nodes: dict[int, dict] = {
        id(c): {"name": c.name, "type": c.span_type, "latency_ms": c.latency_ms, "children": []}
        for c in children
    }
    roots: list[dict] = []
    for c in children:
        p = parent_map.get(id(c))
        if p is None:
            roots.append(nodes[id(c)])
        else:
            nodes[p]["children"].append(nodes[id(c)])
    return roots


def print_tree(nodes: list[dict], indent: int = 0) -> None:
    for n in nodes:
        print("  " * indent + f"|- {n['name']}  [{n['type']}]  {n['latency_ms']}ms")
        print_tree(n["children"], indent + 1)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")
    observer = get_observer()
    parent_map: dict[int, int | None] = {}

    with observer.trace(
        "lead_agent_turn",
        user_id="user_demo_001",
        session_id="sess_demo_001",
        metadata={"entry": "canvas"},
    ) as trace:
        # 层级 ①：意图识别 → LLM 调用
        with observe_llm_call(trace, "intent_classify", messages=[{"role": "user", "content": "分析销量"}]) as s:
            parent_map[id(s)] = None          # 直接挂在 trace 下
            time.sleep(0.01)
            s.update(output='{"intent": "analysis"}')
        # 层级 ①：决策 → LLM 调用
        with observe_llm_call(trace, "decide_action", messages=[{"role": "user", "content": "分析销量"}]) as s:
            parent_map[id(s)] = None
            time.sleep(0.01)
            s.update(output='{"action": "call_analysis"}')

        # 层级 ②：run_analysis（chain 型，忠实 lead_tools 的命名/类型）
        chain = trace.span(name="run_analysis", span_type="chain")
        parent_map[id(chain)] = None
        chain.input = {"goal": "按区域聚合销量"}
        run_start = time.time()
        # 层级 ③：chain 内部的步骤级 span
        with observe_tool_call(trace, "query_sql", args={"sql": "SELECT region, SUM(amount) FROM orders"}) as s:
            parent_map[id(s)] = id(chain)     # 挂在 run_analysis 下
            time.sleep(0.05)
            s.update(output='{"rows": 5}')
        with observe_tool_call(trace, "render_chart", args={"chart_type": "bar"}) as s:
            parent_map[id(s)] = id(chain)
            time.sleep(0.01)
            s.update(output='{"ok": true}')
        step_stats = trace.span(name="step_stats", span_type="chain")
        parent_map[id(step_stats)] = id(chain)
        step_stats.input = {"steps": 2, "ok": 2, "fail": 0}
        chain.update(output={"latency_ms": int((time.time() - run_start) * 1000), "blocks_added": 1})
        chain.finish()
        step_stats.finish()

    print("\n扁平 children（真实存储）：")
    for i, c in enumerate(trace.children, 1):
        print(f"  #{i} {c.span_type:10s} {c.name:<18s} {c.latency_ms:>4d}ms")

    print("\n重建出的层级树：")
    tree = rebuild_tree(trace.children, parent_map)
    print_tree(tree)

    print("\n层级核对：")
    by_type = {}
    for c in trace.children:
        by_type[c.span_type] = by_type.get(c.span_type, 0) + 1
    print(f"  generation(LLM) ×{by_type.get('generation', 0)} / chain ×{by_type.get('chain', 0)} / tool ×{by_type.get('tool', 0)}")
    print("  → 层级结构由 parent_map 重建；Langfuse 启用后 SDK 直接真实嵌套，无需重建")


if __name__ == "__main__":
    main()