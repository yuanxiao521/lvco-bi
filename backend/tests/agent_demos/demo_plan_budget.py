"""D2.4 决策 · LLM 预算封顶（_ensure_llm_budget）。

目标：演示"为什么要有全局 LLM 预算"——没有预算，一个失控计划能把 token 烧穿。
公式（agent_orchestrator.py）：budget = 1(planner) + 步骤数×3(单步上限) + 1(report) + 2(余量)

观察点：
- 不同步数 → 不同预算
- counter 为空 → 回退固定上限 _MAX_LLM_CALLS_PER_TASK
- 模拟"每步最多 5 次 LLM 调用"的失控计划，看何时被跳过

运行（在 backend 目录下）：
    python tests/agent_demos/demo_plan_budget.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.agent_orchestrator import (  # noqa: E402
    _MAX_LLM_CALLS_PER_TASK,
    _ensure_llm_budget,
    _llm_budget,
)


def budget_for(n_steps: int) -> int:
    counter = {"count": 0}
    steps = [{"id": f"s{i}"} for i in range(n_steps)]
    _ensure_llm_budget(counter, steps)
    return _llm_budget(counter)


def main() -> None:
    print("① 预算随步数变化（公式 1 + 步骤×3 + 1 + 2）")
    for n in (0, 1, 3, 5, 10):
        print(f"    步骤数={n:>2} → budget={budget_for(n)}")
    print()

    print(f"② counter 为空 → 回退固定上限 budget={_llm_budget(None)}"
          f"（_MAX_LLM_CALLS_PER_TASK={_MAX_LLM_CALLS_PER_TASK}）")
    print()

    print("③ 失控计划模拟：10 步、每步想调 5 次 LLM")
    steps = [{"id": f"s{i}"} for i in range(10)]
    counter = {"count": 0}
    _ensure_llm_budget(counter, steps)
    budget = _llm_budget(counter)
    print(f"    budget={budget}")
    per_step_want = 5
    for step in steps:
        if counter["count"] >= budget:
            print(f"    {step['id']} 被跳过（已用 {counter['count']}/{budget}）")
            continue
        for _ in range(per_step_want):
            if counter["count"] >= budget:
                break
            counter["count"] += 1
        print(f"    {step['id']} 执行后 count={counter['count']}/{budget}")
    print(f"    最终 count={counter['count']}，未超过 budget={budget} ✅")


if __name__ == "__main__":
    main()
