# -*- coding: utf-8 -*-
"""双跑对拍汇总：调用次数 + 逐题分数对比。"""
import json, re, sys
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(r"E:\BI\LvcoBI\lvco-bi\backend")
LOGS = ROOT / "eval_logs"


def calls(tag: str) -> tuple[int, int]:
    """返回 (总调用次数, 批次数)。"""
    total, batches = 0, 0
    for f in sorted(LOGS.glob(f"{tag}*.log")):
        if re.search(r"r\d+\.log$", f.name):
            continue  # 重试批不计入首轮
        total += len(re.findall(r"\[llm\] call model=", f.read_text(encoding="utf-8", errors="replace")))
        batches += 1
    return total, batches


def scores(tag: str) -> dict:
    out = {}
    for f in sorted((ROOT / "tests/agent_evals").glob(f"semantic_results_{tag}*.jsonl")):
        if re.search(r"r\d+\.jsonl$", f.name):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                j = json.loads(line)
                out[j["question_id"]] = j
    return out


lu, bu = calls("u")
ll, bl = calls("l")
su, sl = scores("u"), scores("l")

print("=== 调用次数（LLM 调用总量，含执行器内部）===")
print(f"  unified: {lu} 次 / {len(su)} 题 = {lu/len(su):.2f} 次/题  ({bu} 批)")
print(f"  legacy : {ll} 次 / {len(sl)} 题 = {ll/len(sl):.2f} 次/题  ({bl} 批)")
print(f"  差值   : {lu - ll:+d} 次（{(lu-ll)/ll*100:+.1f}%）")

print()
print("=== 达成率 ===")
for name, s in (("unified", su), ("legacy", sl)):
    hit = sum(round(v["score"] * v["reachable"]) for v in s.values())
    reach = sum(v["reachable"] for v in s.values())
    print(f"  {name}: 加权 {hit}/{reach} = {hit/reach*100:.1f}%  全过 {sum(1 for v in s.values() if v['overall'])}/{len(s)}")

print()
print("=== 逐题对比（只列不同的）===")
diff = 0
for q in sorted(set(su) | set(sl)):
    a, b = su.get(q), sl.get(q)
    if a is None or b is None:
        print(f"  {q}: 缺数据 unified={a is not None} legacy={b is not None}")
        continue
    if abs(a["score"] - b["score"]) > 1e-9:
        diff += 1
        print(f"  {q}: unified={a['score']:.2f}  legacy={b['score']:.2f}")
print(f"  共 {diff} 题不同；{len(su)-diff} 题相同")
