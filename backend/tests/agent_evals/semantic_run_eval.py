"""语义评测 v2 运行入口：结构化语义期待判分（三层查询路径统一）。

用法：
    cd backend
    python tests/agent_evals/semantic_run_eval.py            # 13 题，真实 Lead 全链路
    python tests/agent_evals/semantic_run_eval.py --dry      # 只看判分映射（mock events）

输出：
    tests/agent_evals/semantic_report.md    语义评测报告
    tests/agent_evals/semantic_results.jsonl  每题结果
    tests/agent_evals/semantic_events.jsonl   事件存档（可离线重放）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from tests.agent_evals.semantic_judge import (  # noqa: E402
    SemanticResult,
    extract_canvas_metrics,
    extract_user_query,
    iter_successful_calls,
    iter_successful_chart_types,
    judge_canvas_semantic,
    judge_semantic,
)

BASE = Path(__file__).parent
DATASET_V2 = BASE / "dataset_v2.jsonl"
OUT_MD = BASE / "semantic_report.md"
OUT_RESULTS = BASE / "semantic_results.jsonl"
OUT_EVENTS = BASE / "semantic_events.jsonl"


def load_dataset(path: Path) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))
    return items


def extract_chart_type(events: list[dict]) -> str:
    for e in reversed(events):
        if e.get("type") == "chart":
            return str(e.get("chart_type") or "")
    return ""


def extract_final_response(events: list[dict]) -> str:
    """最终回答的取证：lead 的话（type=text）+ 画布叙事块内容（add_text_block 成功）。

    画布场景下结论真正落在文本块里，lead 收尾常是模板句（"已在画布上添加了 N 个
    内容块"）不含要点，只取 lead 的话会把回答判漏。把成功落盘的文本块内容并入，
    判定"用户从交付物里能否拿到要点"才符合画布语义。
    """
    parts = [str(e.get("content") or "") for e in events
             if e.get("type") == "text" and e.get("content")]
    for args, ok in iter_successful_calls(events, "add_text_block"):
        if not ok:
            continue
        content = args.get("content")
        if content:
            parts.append(str(content))
    return "".join(parts)


def extract_block_chart_types(events: list[dict]) -> list[str]:
    """已成功落块的图表类型（add_chart_block 执行成功才算）。"""
    return iter_successful_chart_types(events)


def count_blocks(events: list[dict]) -> int:
    """成功落盘的块数（图表块 + 文本块 + 成功改动的已有块）。

    update_chart_block 修改的是已有块，改图类任务（改维度/改类型/调粒度）没有
    add 调用，只数 add 会把"改了图但没新增"的题误判为零落块——成功 update
    同样代表一次画布交付，一并计入。
    """
    return (sum(1 for _ in iter_successful_calls(events, "add_chart_block"))
            + sum(1 for _ in iter_successful_calls(events, "add_text_block"))
            + sum(1 for _ in iter_successful_calls(events, "update_chart_block")))


# 数据源标记 → 评测账号下的数据源 id（B 部分：双数据集泛化验证）
DS_IDS = {
    "ecommerce": "a51b7f71-1c4e-41c1-a58f-044f3891ba1b",
    "product": "4de1640b-8712-4076-8e51-6dc3cab535d6",
}


async def prepare_attempt(q: dict, user_id: str, entry: str, dry: bool, timeout: float = 0.0):
    if dry:
        # 不调 LLM：返回空事件（仅验证判分链路可跑）
        return _as_attempt(None)
    from tests.agent_evals.run_eval import run_agent

    selected = DS_IDS.get(q.get("ds") or "ecommerce")
    if timeout > 0:
        try:
            return _as_attempt(await asyncio.wait_for(
                run_agent(q, user_id=user_id, mode="real", entry=entry,
                          selected_datasource_id=selected),
                timeout=timeout,
            ))
        except asyncio.TimeoutError:
            return _as_attempt(None, error=f"timeout>{timeout:.0f}s")
    return _as_attempt(await run_agent(q, user_id=user_id, mode="real", entry=entry,
                                       selected_datasource_id=selected))


def _as_attempt(obj, error: str | None = None) -> dict:
    """把 AttemptTrace（dataclass）或 dict 归一为 eval 内部统一 dict 结构。"""
    base = {"events": [], "error": error, "iteration_count": 0}
    if obj is None:
        return base
    if isinstance(obj, dict):
        base.update(obj)
        return base
    # dataclass（AttemptTrace 等）
    base["events"] = getattr(obj, "events", []) or []
    base["error"] = getattr(obj, "error", None) or error
    base["iteration_count"] = getattr(obj, "iteration_count", 0) or 0
    base["question_id"] = getattr(obj, "question_id", None)
    base["final_response"] = getattr(obj, "final_response", "") or ""
    return base


def judge(q: dict, attempt: dict):
    events = attempt.get("events") or []
    final_response = extract_final_response(events)
    if q.get("category") == "canvas":
        return judge_canvas_semantic(
            q,
            extract_canvas_metrics(events),
            block_chart_types=extract_block_chart_types(events),
            block_count=count_blocks(events),
            final_response=final_response,
            events=events,
        )
    return judge_semantic(
        q,
        extract_user_query(events),
        chart_type=extract_chart_type(events),
        final_response=final_response,
    )


def render_report(results: list, questions: list[dict], duration: float) -> str:
    n = len(results)
    avg = sum(r.score for r in results) / n if n else 0.0
    full = sum(1 for r in results if r.overall)
    by_sub = {
        k: sum(1 for r in results if getattr(r, k))
        for k in ("matched_metrics", "matched_dims", "matched_filters",
                  "matched_top_n", "matched_chart", "matched_answer")
    }
    sub_tot = {
        "matched_metrics": sum(1 for r in results if "metrics" in " ".join(r.details) or True),
        "matched_dims": sum(1 for r in results if any("维度" in d or "dims" in d for d in r.details)),
        "matched_filters": sum(1 for r in results if any("过滤" in d or "filters" in d for d in r.details)),
        "matched_top_n": sum(1 for r in results if any("top_n" in d or "top" in d.lower() or "block_count" in d for d in r.details)),
        "matched_chart": sum(1 for r in results if any("chart" in d or "图表" in d for d in r.details)),
        "matched_answer": sum(1 for r in results if any("回答" in d or "answer" in d for d in r.details)),
    }
    lines = [
        "# LvcoBI 语义评测报告（v2 结构化语义期待）",
        "",
        f"**生成时间**：{time.strftime('%Y-%m-%d %H:%M:%S')} ｜ 数据集：`dataset_v2.jsonl`（{n} 道）｜ 总耗时 {duration:.1f}s",
        "",
        "## 总分",
        "",
        f"| 项 | 值 |",
        f"|---|---|",
        f"| 平均语义达成率 | **{avg*100:.1f}%** |",
        f"| 全部子项达成的题 | {full}/{n} |",
        "",
        "## 子项达成率（每题的判定是『期望语义 ⊆ 实际查询语义』，未要求=不计分）",
        "",
        "| 子项 | 达成/可判 | 达成率 |",
        "|---|---|---|",
    ]
    names = [("matched_metrics", "指标口径"), ("matched_dims", "维度/粒度"), ("matched_filters", "过滤/时间口径"),
             ("matched_top_n", "排序限量/块数"), ("matched_chart", "图表类型"), ("matched_answer", "回答要点/叙事")]
    for key, label in names:
        t = sub_tot.get(key, 0) or 1
        lines.append(f"| {label} | {by_sub[key]}/{t} | {by_sub[key]/t*100:.0f}% |")
    lines.append("")

    # 按路径分组
    lines.append("## 按查询路径分组")
    lines.append("")
    lines.append("| 路径 | 题数 | 平均达成 | 全过 |")
    lines.append("|---|---|---|---|")
    from collections import OrderedDict
    groups = OrderedDict()
    for q, r in zip(questions, results):
        groups.setdefault(q.get("path", "?"), []).append(r)
    for path, rs in groups.items():
        grp_avg = sum(x.score for x in rs) / len(rs)
        grp_full = sum(1 for x in rs if x.overall)
        lines.append(f"| {path} | {len(rs)} | {grp_avg*100:.1f}% | {grp_full}/{len(rs)} |")
    lines.append("")

    # 每题明细
    lines.append("## 每题明细")
    lines.append("")
    lines.append("| 题 | 路径 | 达成分 | 结论 | 说明 |")
    lines.append("|---|---|---|---|---|")
    for q, r in zip(questions, results):
        status = "✅" if r.overall else "🟡" if r.score >= 0.5 else "❌"
        lines.append(f"| {r.question_id} | {q.get('path','')} | {r.passed}/{r.reachable} | {status} | "
                     f"{' | '.join(r.details[:6])[:200]} |")
    lines.append("")
    return "\n".join(lines)


async def _wait_db_available(timeout: float = 60.0) -> None:
    """探测 lvco_bi.duckdb 是否被其他进程独占；占用则等待重试，避免并发评测互相踩锁。

    DuckDB 单写锁：一个进程以读写打开时，其他进程打不开（Windows 上连只读副本都无法复制）。
    评测与分析应串行使用同一数据文件。超时仍被占用则报错退出，给出明确指引。
    """
    import duckdb
    from app.config import settings

    db_path = Path(settings.DUCKDB_DATA_DIR) / "lvco_bi.duckdb"
    deadline = time.time() + timeout
    while True:
        try:
            conn = duckdb.connect(str(db_path), read_only=True)
            conn.close()
            return
        except duckdb.Error as e:
            if "already open" not in str(e).lower():
                raise
            if time.time() >= deadline:
                raise RuntimeError(
                    f"lvco_bi.duckdb 被其他进程占用已超 {timeout:.0f}s，仍无法获取只读连接。"
                    "请先停止占用方（正在运行的后端服务，或另一个评测进程）再重试。"
                    "评测必须在无其他进程读写 DuckDB 时串行运行。"
                ) from e
            print(f"  [db-busy] lvco_bi.duckdb 被占用，{int(deadline - time.time())}s 后超时，每 5s 重试…", flush=True)
            await asyncio.sleep(5)


async def main_async(args) -> int:
    if not args.dry:
        await _wait_db_available()
    ds = load_dataset(DATASET_V2)

    # 画布题全部保留：--only-canvas 则只测全部画布题（不剔除、不轮换原三道的任何一道）。
    canvas_cases = [q for q in ds if q.get("category") == "canvas"]
    if args.only_canvas:
        ds = canvas_cases
        print(f"仅画布：运行全部 {len(ds)} 道画布题 {sorted(q['id'] for q in ds)}")
    else:
        print(f"全量：{len(ds)} 题（画布 {len(canvas_cases)} 道 + 对话 {len(ds)-len(canvas_cases)} 道）")
    results: list[SemanticResult] = []
    start = time.time()

    # 每题独立超时：画布任务多步编排较慢，给足时间；对话题收紧避免挂死拖垮全流程
    by_path = {q["id"]: q.get("path", "") for q in ds}
    timeout_of = lambda qid: 360.0 if by_path.get(qid) == "canvas" else 150.0

    # 增量落盘：逐题追加，任一楼挂起也保留前面的结果
    with open(OUT_RESULTS, "w", encoding="utf-8") as fres, \
         open(OUT_EVENTS, "w", encoding="utf-8") as fev:
        for i, q in enumerate(ds, 1):
            entry = "canvas" if q.get("category") == "canvas" else "chat"
            t0 = time.time()
            attempt = await prepare_attempt(q, args.user_id, entry, args.dry,
                                            timeout=0.0 if args.dry else timeout_of(q["id"]))
            r = judge(q, attempt)
            results.append(r)
            fres.write(json.dumps({
                "question_id": r.question_id, "score": r.score, "overall": r.overall,
                "passed": r.passed, "reachable": r.reachable, "details": r.details,
            }, ensure_ascii=False) + "\n")
            fres.flush()
            fev.write(json.dumps({
                "question_id": q["id"], "path": q.get("path", ""), "query": q.get("query", ""),
                "error": attempt.get("error"), "iteration_count": attempt.get("iteration_count", 0),
                "events": attempt.get("events", []),
            }, ensure_ascii=False) + "\n")
            fev.flush()
            el = time.time() - t0
            print(f"[{i}/{len(ds)}] {q['id']} path={q.get('path','')} 耗时{el:.0f}s "
                  f"err={attempt.get('error')} score={r.passed}/{r.reachable} "
                  f"metrics={'✓' if r.matched_metrics else '✗'} dims={'✓' if r.matched_dims else '·'} "
                  f"filters={'✓' if r.matched_filters else '·'} top_n={'✓' if r.matched_top_n else '·'} "
                  f"chart={'✓' if r.matched_chart else '✗'} answer={'✓' if r.matched_answer else '✗'}")
            print(f"      {r.details[:5]}", flush=True)

    duration = time.time() - start
    md = render_report(results, ds, duration)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"\n报告: {OUT_MD}")
    print(f"总耗时 {duration:.1f}s | 平均达成 {sum(r.score for r in results)/len(results)*100:.1f}% | 全过 {sum(1 for r in results if r.overall)}/{len(results)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="语义评测 v2")
    p.add_argument("--dry", action="store_true", help="不调 LLM，仅验证判分链路")
    p.add_argument("--user-id", default="21bee02f-dcb3-4108-b721-d8448db678e4")
    p.add_argument("--only-canvas", action="store_true", help="仅跑画布题（全部保留，不轮换），跳过对话题")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())