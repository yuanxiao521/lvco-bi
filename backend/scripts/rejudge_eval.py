# -*- coding: utf-8 -*-
"""免重跑重判分：用已存的评测事件重新判分（改判分规则时用，省一次 20+ 分钟 LLM 跑）。

用法：
    cd backend
    .venv/Scripts/python.exe scripts/rejudge_eval.py [events文件] [--limit N]

默认 events 文件 tests/agent_evals/eval_results_events.jsonl（v1 基线）。
为什么需要：判分器一旦修正（图表等价类、成功查询提取、可评分分母…），历史结果会变；
重判分让"同一批事件、不同判分规则"可比，而不必再花一次模型调用。
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(".").resolve()))

from tests.agent_evals.judge import AttemptTrace, aggregate_results, judge_attempt  # noqa: E402
from tests.agent_evals.run_eval import build_sql_executor  # noqa: E402

BASE = pathlib.Path("tests/agent_evals")
USER = "21bee02f-dcb3-4108-b721-d8448db678e4"


def _final_text(evs: list[dict]) -> str:
    """从事件流重建最终回答（report 优先，其次拼接 text）。"""
    txt = ""
    for e in evs:
        if e.get("type") == "report" and e.get("content"):
            return e["content"]
        if e.get("type") == "text" and e.get("content"):
            txt += e["content"]
    return txt


async def _main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = 0
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    events_path = pathlib.Path(args[0]) if args else BASE / "eval_results_events.jsonl"

    dataset = [
        json.loads(l)
        for l in (BASE / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    events = [
        json.loads(l)
        for l in events_path.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    by_id = {e["question_id"]: e for e in events}
    questions = dataset[:limit] if limit else dataset

    rows = []
    for q in questions:
        rec = by_id.get(q["id"])
        if rec is None:
            continue
        evs = rec.get("events") or []
        attempt = AttemptTrace(
            question_id=q["id"],
            user_msg=q.get("query", ""),
            events=evs,
            final_response=_final_text(evs),
            error=rec.get("error"),
            iteration_count=rec.get("iteration_count") or 0,
        )
        executor = await build_sql_executor(attempt, q.get("expected_datasource"), user_id=USER)
        rows.append(judge_attempt(q, attempt, sql_executor=executor))

    s = aggregate_results(rows)
    print(f"事件文件: {events_path}")
    print(f"题目数: {s['total']}  可评分: {s.get('sql_scorable_count', s.get('sql_scorable'))}")
    print(f"SQL 准确率: {s['sql_accuracy_count']}/{s.get('sql_scorable_count', s['total'])} = {s['sql_accuracy_rate']*100:.1f}%")
    print(f"图表类型: {s['chart_type_correct_count']}/{s['total']} = {s['chart_type_correct_rate']*100:.1f}%")
    print(f"工具成功: {s['tool_success_count']}/{s['total']}")
    print(f"输出有效: {s['final_response_valid_count']}/{s['total']}")
    print(f"平均迭代: {s['avg_iterations']}")
    for r in rows:
        note = (r.notes[:1] or ["-"])[0]
        print(f"  {r.question_id} sql={int(r.sql_accuracy)} chart={int(r.chart_type_correct)} "
              f"{r.expected_chart_type}->{r.actual_chart_type} | {note[:70]}")


asyncio.run(_main())
