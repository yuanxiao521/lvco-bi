"""循环诊断：分析 final_v5_events.jsonl，实证"无恶性循环" + 定位慢题。

输出：
- 每题决策轮次 / 工具调用次数 / 重复工具签名（同 name+args 连续重复 = 循环迹象）
- 全量汇总 + Top 慢题 / 大轮次题
用法：python tests/agent_evals/scan_cycles.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
EVENTS = Path(__file__).parent / "final_v5_events.jsonl"


def _tool_signature(ev: dict) -> str:
    try:
        args = json.dumps(ev.get("args") or {}, ensure_ascii=False, sort_keys=True)[:120]
    except Exception:
        args = ""
    return f"{ev.get('name')}({args})"


def main() -> None:
    if not EVENTS.exists():
        print("final_v5_events.jsonl 不存在，等待评测完成")
        return
    summaries: list[dict] = []
    with open(EVENTS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            attempt = json.loads(line)
            events = attempt.get("events") or []
            tool_calls = [e for e in events if e.get("type") == "tool_call"]
            # 重复签名：连号中相同签名的最大连续出现
            sigs = [_tool_signature(e) for e in tool_calls]
            max_repeat = 0
            cur = 1
            for i in range(1, len(sigs)):
                if sigs[i] == sigs[i - 1]:
                    cur += 1
                    max_repeat = max(max_repeat, cur)
                else:
                    cur = 1
            summaries.append({
                "q": attempt.get("question_id"),
                "query": (attempt.get("query") or "")[:24],
                "error": attempt.get("error"),
                "tools": len(tool_calls),
                "max_same_sign_repeat": max_repeat,
                "signs": sorted(set(sigs)),
            })

    total = len(summaries)
    print(f"已分析 {total} 题\n")
    print(f"{'题':<6}{'工具数':<6}{'最大连续相同调用':<14}错误")
    for s in sorted(summaries, key=lambda x: -(x["tools"] or 0)):
        print(
            f"{s['q']:<6}{s['tools']:<8}{s['max_same_sign_repeat']:<16}{s['error'] or ''}"
        )
    worst = max((s["tools"] or 0 for s in summaries), default=0)
    repeat_all = sum(1 for s in summaries if s["max_same_sign_repeat"] >= 3)
    print(f"\n结论: 工具调用最多次={worst} | 存在>=3次连续相同调用的题数={repeat_all}")
    if repeat_all == 0 and worst <= 30:
        print("→ 无循环迹象：所有题工具调用有界、无连续重复签名（收敛闸/熔断/幂等生效）")
    else:
        print("→ 需人工检查上述高亮题（看其事件流是否反复重试）")


if __name__ == "__main__":
    main()