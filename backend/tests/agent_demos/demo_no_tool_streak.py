"""D3.4 感知 · 连续无工具调用 → goal 达成校验触发。

目标：演示画布编排器的"目标达成校验"。
画布步骤的唯一达成判据 = 本轮产出了落块工具调用（canvas_action）。
若 LLM 连续 `_MAX_NO_TOOL_STREAK` 轮只输出文字不调工具 → 判定未达成，跳过该步骤。

对应 lead-agent-upgrade-design.md §4.8 的遗留修复。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_no_tool_streak.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.canvas_orchestrator import _MAX_NO_TOOL_STREAK  # noqa: E402


def simulate(tool_rounds: set[int], max_rounds: int = 5) -> list[str]:
    """模拟一个步骤的多轮 ReAct：tool_rounds 里的轮次会调用工具，其余轮次不调。"""
    log: list[str] = []
    no_tool_streak = 0
    for r in range(1, max_rounds + 1):
        if r in tool_rounds:
            no_tool_streak = 0
            log.append(f"round {r}: 调用了落块工具 → streak 归零，步骤达成 ✅")
            return log
        no_tool_streak += 1
        if no_tool_streak >= _MAX_NO_TOOL_STREAK:
            log.append(
                f"round {r}: 无工具调用（streak={no_tool_streak} ≥ {_MAX_NO_TOOL_STREAK}）"
                f" → 跳过步骤，skipped_reason='连续未调用画布落块工具，goal 未达成'"
            )
            return log
        log.append(
            f"round {r}: 无工具调用（streak={no_tool_streak}）"
            f" → 回灌强制调用提示，继续"
        )
    log.append("达到最大轮次，未产出落块")
    return log


def main() -> None:
    print(f"_MAX_NO_TOOL_STREAK = {_MAX_NO_TOOL_STREAK}\n")

    print("场景 A：一直不调工具（第 1、2 轮都纯文字）")
    for line in simulate(tool_rounds=set()):
        print(f"  {line}")
    print()

    print("场景 B：第 2 轮调了工具（逃逸一次后被拉回）")
    for line in simulate(tool_rounds={2}):
        print(f"  {line}")
    print()

    print("场景 C：第 1 轮就调工具（正常路径）")
    for line in simulate(tool_rounds={1}):
        print(f"  {line}")
    print()

    print("观察点：")
    print("  - 阈值 2：给模型一次自我纠错机会，连续两次逃逸才跳过")
    print("  - 跳过的步骤带 skipped_reason，便于前端/报告解释'为什么少了这块'")


if __name__ == "__main__":
    main()
