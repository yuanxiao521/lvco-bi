# -*- coding: utf-8 -*-
"""分批评测驱动：3 道一批 + 遇 LLM 故障自动重试 + 汇总（对齐任务书的"加权 76/85"口径）。

背景：上游 LLM 会间歇性返回 502/503（实测一轮里 20 道连跑必崩），
所以不能一次跑满 20 道——分批 + 对失败题重试，才能拿到可用数据。
"""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "tests" / "agent_evals" / "semantic_run_eval.py"
OUTDIR = ROOT / "tests" / "agent_evals"

BATCHES = [
    ["V011", "V012", "V013"],
    ["V014", "V015", "V016"],
    ["V017", "V018", "V019"],
    ["V020", "P001", "P002"],
    ["P003", "P004", "P005"],
    ["P006", "P007", "P008"],
    ["P009", "P010"],
]
MAX_RETRY = 2
LLM_FAIL_MARKS = ("LLM 502", "LLM 503", "LLM 429", "upstream unreachable")


def run(ids: list[str], tag: str):
    cmd = [sys.executable, str(EVAL), "--only-canvas",
           "--ids", ",".join(ids), "--out-prefix", tag]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    log = (p.stdout or "") + (p.stderr or "")
    res = OUTDIR / f"semantic_results_{tag}.jsonl"
    rows = []
    if res.exists():
        for line in res.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    llm_failed = any(m in log for m in LLM_FAIL_MARKS)
    print(f"  [{tag}] {len(rows)} 题, 耗时 {time.time()-t0:.0f}s, LLM故障={llm_failed}", flush=True)
    return {r["question_id"]: r for r in rows}, llm_failed


def main() -> int:
    best: dict[str, dict] = {}
    attempts: dict[str, int] = {}

    for bi, ids in enumerate(BATCHES, 1):
        todo = list(ids)
        for attempt in range(MAX_RETRY + 1):
            if not todo:
                break
            tag = f"b{bi}" if attempt == 0 else f"b{bi}r{attempt}"
            print(f"批次 {bi} 第 {attempt+1} 次：{todo}", flush=True)
            rows, llm_failed = run(todo, tag)
            still = []
            for qid in todo:
                r = rows.get(qid)
                attempts[qid] = attempts.get(qid, 0) + 1
                if r is None:
                    still.append(qid)
                    continue
                prev = best.get(qid)
                # 取分数更高的一次（LLM 故障那次的 0 分不应算作真实能力）
                if prev is None or r["score"] > prev["score"]:
                    best[qid] = r
                # 分数为 0 且这一轮确有 LLM 故障 → 重试
                if r["score"] == 0 and llm_failed:
                    still.append(qid)
            todo = still
        if todo:
            print(f"  批次 {bi} 仍失败：{todo}", flush=True)
        time.sleep(5)

    # ── 汇总（口径对齐任务书：加权达成 = Σscore / Σreachable）──
    order = [q for b in BATCHES for q in b]
    got = [best[q] for q in order if q in best]
    # 口径（与任务书"加权 76/85"一致）：
    #   score 是 **0~1 的比例**，reachable 是**可核验点数**
    #   → 加权达成 = Σ(score × reachable) / Σreachable
    # 踩过的坑：写成 Σscore/Σreachable 会得到 22.2% 这种荒谬值（分子是比例、分母是点数）。
    hit = sum(round(r["score"] * r["reachable"]) for r in got)
    reach = sum(r["reachable"] for r in got)
    mean_pct = (sum(r["score"] for r in got) / len(got) * 100) if got else 0.0
    allpass = sum(1 for r in got if r["overall"])
    print("\n================ 汇总 ================")
    for q in order:
        r = best.get(q)
        if r is None:
            print(f"  {q}: 未拿到结果")
        else:
            print(f"  {q}: {r['score']}/{r['reachable']}  overall={r['overall']}  尝试{attempts.get(q,0)}次")
    print(f"\n加权达成 = {hit}/{reach} = {hit/reach*100:.1f}%" if reach else "无可达项")
    print(f"平均达成 = {mean_pct:.1f}%")
    print(f"全过 = {allpass}/{len(order)}")
    print(f"需重试的题 = {[q for q in order if attempts.get(q,0) > 1]}")
    (OUTDIR / "semantic_batched_summary.json").write_text(json.dumps({
        "weighted_hit": hit, "weighted_reachable": reach,
        "weighted_pct": round(hit / reach * 100, 1) if reach else None,
        "mean_pct": round(mean_pct, 1),
        "all_pass": allpass, "total": len(order),
        "per_question": {q: best.get(q) for q in order},
        "attempts": attempts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细: tests/agent_evals/semantic_batched_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
