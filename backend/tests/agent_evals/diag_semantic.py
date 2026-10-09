"""真实评测诊断：跑少量问题，打印完整事件流 + 判分，定位系统问题。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from tests.agent_evals.semantic_judge import (
    extract_canvas_metrics, extract_user_query, judge_canvas_semantic, judge_semantic,
)
from tests.agent_evals.semantic_run_eval import (
    DATASET_V2, DS_IDS, count_blocks, extract_block_chart_types, extract_chart_type,
    extract_final_response, load_dataset,
)
from tests.agent_evals.run_eval import run_agent

IDS_SELECT = ["P001", "P002", "P003", "P004"]  # 全量回归：验证记忆/观测/评测器改动不破坏功能


def _compact(ev: dict) -> dict:
    e = dict(ev)
    for k in ("args", "result", "content", "reason"):
        if isinstance(e.get(k), str) and len(e.get(k, "")) > 200:
            e[k] = e[k][:200] + f"...[{len(e[k])}ch]"
    return e


async def main() -> int:
    ds = load_dataset(DATASET_V2)
    for i, q in enumerate(ds, 1):
        if q["id"] not in IDS_SELECT:
            continue
        entry = "canvas" if q["category"] == "canvas" else "chat"
        t0 = time.time()
        attempt = await run_agent(q, user_id="21bee02f-dcb3-4108-b721-d8448db678e4",
                                  mode="real", entry=entry,
                                  selected_datasource_id=DS_IDS.get(q.get("ds") or "ecommerce"))
        el = time.time() - t0
        print("\n" + "=" * 90)
        print(f"[{q['id']}] path={q['path']} query={q['query']} 耗时{el:.0f}s "
              f"error={attempt.error} iter={attempt.iteration_count}")
        print("-" * 90)
        for ev in attempt.events:
            print(json.dumps(_compact(ev), ensure_ascii=False))
        print("-" * 90)
        # 判分
        if q["category"] == "canvas":
            uq = extract_canvas_metrics(attempt.events)
            r = judge_canvas_semantic(q, uq,
                                      block_chart_types=extract_block_chart_types(attempt.events),
                                      block_count=count_blocks(attempt.events),
                                      final_response=extract_final_response(attempt.events),
                                      events=attempt.events)
            print("画布 metrics:", json.dumps(uq, ensure_ascii=False))
        else:
            uq = extract_user_query(attempt.events)
            r = judge_semantic(q, uq, chart_type=extract_chart_type(attempt.events),
                               final_response=extract_final_response(attempt.events))
            print("归一语义:", json.dumps(vars(uq), ensure_ascii=False, default=str))
        print(f">> 判分 {r.passed}/{r.reachable} overall={r.overall}")
        for d in r.details:
            print("   ", d)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))