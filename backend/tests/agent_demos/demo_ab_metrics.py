"""D5.4 监控 · 从两份运行结果（off/on）计算 AB 对照 6 项指标。

口径与 tests/agent_evals/ab_experiment.py 的 _rate / _agg / _fmt_pct / _fmt_num 完全一致：
- 报告完整率：report_ok 为 True 的比例
- 落块数：blocks 的均值（on 轮来自 run_analysis 的 blocks_added）
- 意图准确率：intent_ok 为 True 的比例（旧路径无 intent 事件 → None → 记 N/A）
- 平均步数：steps 的均值
- 平均耗时：duration_s 的均值
- 降级率：degraded 为 True 的比例（旧路径无该信号 → N/A）

本 demo 用两组合成行演示计算逻辑（真实运行得到 ab_output/ab_off.jsonl、
ab_on.jsonl 后，从 JSON 读入同样行结构即可套用）。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_ab_metrics.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


# ── 与 ab_experiment._rate 一致：布尔列比率，全 None 返回 None ──
def _rate(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v) / len(vals)


# ── 与 ab_experiment._agg 一致：产出 6 项指标 ──
def _agg(rows: list[dict]) -> dict:
    if not rows:
        return {}
    n = len(rows)
    return {
        "report": _rate(rows, "report_ok"),
        "blocks": sum(r["blocks"] for r in rows) / n,
        "intent": _rate(rows, "intent_ok"),
        "steps": sum(r["steps"] for r in rows) / n,
        "dur": sum(r["duration_s"] for r in rows) / n,
        "degraded": _rate(rows, "degraded"),
    }


def _fmt_pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _fmt_num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


# ── 合成行：off（旧路径，无 intent/degraded 信号）+ on（主导 Agent，3 题中 1 题降级）──
OFF_ROWS = [
    {"question_id": "q1", "report_ok": True, "blocks": 1, "intent_ok": None, "steps": 4, "duration_s": 12.3, "degraded": None},
    {"question_id": "q2", "report_ok": True, "blocks": 2, "intent_ok": None, "steps": 6, "duration_s": 18.7, "degraded": None},
    {"question_id": "q3", "report_ok": False, "blocks": 0, "intent_ok": None, "steps": 9, "duration_s": 21.0, "degraded": None},
]
ON_ROWS = [
    {"question_id": "q1", "report_ok": True, "blocks": 1, "intent_ok": True, "steps": 3, "duration_s": 10.1, "degraded": False, "degradations": []},
    {"question_id": "q2", "report_ok": True, "blocks": 2, "intent_ok": True, "steps": 5, "duration_s": 15.4, "degraded": False, "degradations": []},
    {"question_id": "q3", "report_ok": True, "blocks": 1, "intent_ok": True, "steps": 7, "duration_s": 19.9, "degraded": True, "degradations": ["lead_intent_fallback"]},
]


def main() -> None:
    off_a, on_a = _agg(OFF_ROWS), _agg(ON_ROWS)
    print("AB 对照 · 6 项指标汇总\n")
    print("| 指标 | off（旧路径） | on（主导 Agent） |")
    print("|---|---|---|")
    print(f"| 报告完整率 | {_fmt_pct(off_a.get('report'))} | {_fmt_pct(on_a.get('report'))} |")
    print(f"| 落块数（平均） | {_fmt_num(off_a.get('blocks'))} | {_fmt_num(on_a.get('blocks'))} |")
    print(f"| 意图准确率 | {_fmt_pct(off_a.get('intent'))} | {_fmt_pct(on_a.get('intent'))} |")
    print(f"| 平均步数 | {_fmt_num(off_a.get('steps'))} | {_fmt_num(on_a.get('steps'))} |")
    print(f"| 平均耗时(s) | {_fmt_num(off_a.get('dur'))} | {_fmt_num(on_a.get('dur'))} |")
    print(f"| 降级率 | {_fmt_pct(off_a.get('degraded'))} | {_fmt_pct(on_a.get('degraded'))} |")

    print("\n观察点：")
    print("  - off 行 intent/degraded 恒为 None → _rate 返回 None → 表格显示 '-（N/A）'")
    print("  - on 行 3 题中 2 题 report_ok=True 时为 66.7%，本题 3/3 故 100%")
    print("  - degraded 列为 1/3=33.3%，且 degradations 记录具体兜底来源")
    print("  - 真实实验：跑 ab_experiment.py 得到 ab_off.jsonl / ab_on.jsonl 后，")
    print("    用 json 读入同结构行即可直接套用本组函数")


if __name__ == "__main__":
    main()