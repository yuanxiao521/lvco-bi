"""D4.5 记忆 · LeadContext 活记忆与 digest 上限。

目标：演示主导 Agent 的统一记忆载体：
- turns 自动裁剪（LEAD_MAX_TURNS_IN_CTX × 2 条）
- digest() 注入"长期摘要 + 最近轮次"，超长取尾部
- turn_summaries 累积关键节点汇报

运行（在 backend 目录下）：
    python tests/agent_demos/demo_lead_context.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.services.agents.lead.lead_agent import LeadContext  # noqa: E402


def main() -> None:
    cap = max(1, settings.LEAD_MAX_TURNS_IN_CTX) * 2
    print(f"LEAD_MAX_TURNS_IN_CTX={settings.LEAD_MAX_TURNS_IN_CTX} → turns 上限={cap}\n")

    ctx = LeadContext(user_id=1, session_id="sess_demo", history_summary="用户是华东区销售，关注月度趋势。")
    for i in range(1, 51):
        ctx.add_turn("user", f"第 {i} 个问题")
        ctx.add_turn("assistant", f"第 {i} 个回答")

    print(f"追加 50 轮（100 条）后，turns 实际保留：{len(ctx.turns)} 条")
    print(f"最早保留：{ctx.turns[0]['content']}")
    print(f"最新保留：{ctx.turns[-1]['content']}")
    print()

    ctx.turn_summaries.append("已完成：本月销售总额查询")
    ctx.turn_summaries.append("已完成：按地区聚合")
    print(f"关键节点汇报累积：{ctx.turn_summaries}")
    print()

    full = ctx.digest()
    print(f"digest() 默认长度：{len(full)} 字符")
    print("digest() 开头：")
    print("  " + full[:120].replace("\n", "\n  "))
    print()

    short = ctx.digest(max_chars=100)
    print(f"digest(max_chars=100) 长度：{len(short)}（应=100，取尾部）")
    print(f"  → {short!r}")
    print()
    print("观察点：")
    print("  - turns 超上限自动丢最早的（活记忆窗口）")
    print("  - digest 里【长期记忆】永远排最前（只要没被 max_chars 截掉）")
    print("  - 超长时取尾部，保证最近上下文一定在")


if __name__ == "__main__":
    main()
