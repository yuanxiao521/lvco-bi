# -*- coding: utf-8 -*-
"""诊断：unified 比 legacy 多出来的 LLM 调用花在哪。"""
import re, sys
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
LOGS = Path(r"E:\BI\LvcoBI\lvco-bi\backend\eval_logs")

PATTERNS = {
    "LLM 调用总数": r"\[llm\] call model=",
    "派发次数(run_analysis done)": r"run_analysis done",
    "legacy 决策轮(ctx_ledger decision_r)": r"ctx_ledger\] decision_r",
    "回答分支(ctx_ledger answer)": r"ctx_ledger\] answer",
    "planner 规划": r"\[planner\] plan_generation_completed",
    "react 工具调用": r"\[react\] tool_call",
    "编排器步进": r"\[canvas_orchestrator\]|\[agent_orchestrator\]",
}

for tag, name in (("u", "unified"), ("l", "legacy")):
    text = "".join(f.read_text(encoding="utf-8", errors="replace")
                   for f in sorted(LOGS.glob(f"{tag}[0-9]*.log")))
    print(f"=== {name} ===")
    for label, pat in PATTERNS.items():
        n = len(re.findall(pat, text))
        if n:
            print(f"  {label:<40} {n}")
    print()
