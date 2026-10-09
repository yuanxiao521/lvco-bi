"""D3.1 感知 · 枚举 4 个对话阶段各自可用的工具白名单。

目标：理解"按阶段收紧工具集"这一感知机制——同一个 LLM，
在不同 `ConversationPhase` 下看到的工具列表不同，从源头防止跨阶段乱调。

- SELECTING  → 只暴露 list_datasources
- ANALYZING  → 查数 + 数据质量 + 洞察 + 落块
- GENERATING → 图表生成/校验/推荐 + 落块
- REPORTING  → 报告润色 + 落块

运行（在 backend 目录下）：
    python tests/agent_demos/demo_phase_tools.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agent_tools import (  # noqa: E402
    ConversationPhase,
    _PHASE_TOOLS,
    get_tools_for_phase,
)


def build_all_schemas() -> list[dict]:
    names: set[str] = set()
    for allowed in _PHASE_TOOLS.values():
        names |= allowed
    names |= {"query_sql", "render_chart", "polish_text", "list_datasources"}
    return [
        {"type": "function", "function": {"name": n, "description": f"{n} 工具"}}
        for n in sorted(names)
    ]


def main() -> None:
    all_schemas = build_all_schemas()
    print(f"全部工具池（{len(all_schemas)} 个）：{[s['function']['name'] for s in all_schemas]}\n")

    for phase in ConversationPhase:
        tools = get_tools_for_phase(phase, all_schemas)
        names = [s["function"]["name"] for s in tools]
        print(f"阶段 {phase.value:<10} 可用 {len(names)} 个：{names}")

    print("\n观察点：")
    print("  - SELECTING 只给 list_datasources：还没选数据源，别急着查")
    print("  - REPORTING 无查询/图表工具：只负责把已有内容写成报告")
    print("  - 落块工具（add_*_block）在 ANALYZING/GENERATING/REPORTING 都保留")


if __name__ == "__main__":
    main()
