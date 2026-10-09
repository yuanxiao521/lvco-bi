"""D3.3 感知 · render_progress_text 快照（含边界）。

目标：把 StepProgress 渲染成用户可见的一句话，逐种 status 与边界值过一遍。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_progress_render.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.agents.lead.lead_perception import (  # noqa: E402
    StepProgress,
    render_progress_text,
)

CASES = [
    ("进行中（有 note）", StepProgress(index=3, total=7, status="start", note="正在查 2024 年华东区销售额")),
    ("进行中（只有 title）", StepProgress(index=1, total=3, status="start", title="查询本月销售总额")),
    ("进行中（无内容）", StepProgress(index=2, total=5, status="start")),
    ("成功", StepProgress(index=3, total=7, status="ok", note="已拿到 1.2 万行")),
    ("成功（无内容）", StepProgress(index=3, total=7, status="ok")),
    ("失败", StepProgress(index=3, total=7, status="fail", note="查询失败：字段不存在")),
    ("跳过", StepProgress(index=4, total=7, status="skip", note="无数据")),
    ("未知总数（total=0）", StepProgress(index=2, total=0, status="start", note="正在执行工具")),
    ("无索引无总数", StepProgress(index=0, total=0, status="start", note="准备中")),
]


def main() -> None:
    for title, p in CASES:
        print(f"{title:<20} → {render_progress_text(p)}")

    print("\n观察点：")
    print("  - total=0 时退化为【index】；index 也为 0 时不带计数器前缀")
    print("  - note 优先于 title（note 是感知层动态生成的）")
    print("  - 四种 status 各有专属符号：无 / ✓ / ✗ / ·")


if __name__ == "__main__":
    main()
