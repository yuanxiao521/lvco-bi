"""画布「全局计划注入」AB 实验：同一画布题集，注入开关开/关各跑一轮真实编排。

对比维度（每题）：
- canvas_score 及 5 子项（图表数/类型/叙事/布局/顺序）
- 实际落块数、工具调用序列（去重评估是否重复查询）
- 迭代轮数、总耗时

执行（需真实 LLM + 数据库 + 画布数据源）：
    cd backend && python tests/agent_evals/ab_experiment.py --user-id <uid>

输出：
- tests/agent_evals/ab_report.md     题目级对照表 + 汇总
- tests/agent_evals/ab_off.jsonl    注入关闭轮原始结果
- tests/agent_evals/ab_on.jsonl     注入开启轮原始结果
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("ab_experiment")

from tests.agent_evals.judge import judge_attempt  # noqa: E402
from tests.agent_evals.run_eval import load_dataset, run_agent  # noqa: E402

# 输出目录：tests/agent_evals/ab_xxx/
OUT_DIR = Path(__file__).parent / "ab_output"

CANVAS_MARKERS = {
    "canvas_chart_count_ok": "图表数",
    "canvas_chart_types_ok": "类型",
    "canvas_narrative_present": "叙事",
    "canvas_arrange_layout_ok": "布局",
    "canvas_block_order_ok": "顺序",
}
TOOL_LABEL = {
    "add_text_block": "文本块",
    "add_chart_block": "图表块",
    "update_chart_block": "改图表",
    "remove_block": "删块",
    "arrange_layout": "布局",
    "query_engine": "查Q",
    "query_sql": "查SQL",
    "list_datasources": "列源",
    "list_fields": "列字段",
    "render_chart": "出图",
}


def _tool_seq(events: list[dict]) -> list[str]:
    seq = []
    for e in events:
        if e.get("type") == "tool_call" and e.get("name"):
            seq.append(TOOL_LABEL.get(e["name"], e["name"]))
    return seq


def _block_count(events: list[dict]) -> int:
    cnt = 0
    for e in events:
        if e.get("type") == "tool_result":
            raw = e.get("result") or ""
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    continue
            if isinstance(raw, dict) and isinstance(raw.get("canvas_action"), dict):
                cnt += 1
    return cnt


async def _pick_datasource_id(user_id: str) -> str:
    """取测试用户第一个数据源作为选中数据源（注入字段，接近真实画布体验）。"""
    from app.core.database import async_session_factory
    from app.repositories.datasource_repository import SQLAlchemyDataSourceRepository

    async with async_session_factory() as db:
        items, _ = await SQLAlchemyDataSourceRepository(db).list_datasources(
            user_id, page=1, page_size=10, source_type=None, status=None, search=None
        )
        if not items:
            raise RuntimeError(f"user {user_id} 无数据源")
        return str(items[0].id)


class _ForceComplexClassifier:
    """AB 实验强制走编排器：所有画布题都进 CanvasOrchestrator（否则被分类为 simple 走 ReAct，测不到注入）。"""

    def __init__(self) -> None:
        import app.services.ai_service as service_module

        self._module = service_module
        self._orig = service_module.AIService._classify_task_complexity

    async def __call__(self, self_inst, user_msg, degradation_out=None):  # noqa: N805
        return True

    def patch(self) -> None:
        self._module.AIService._classify_task_complexity = self

    def restore(self) -> None:
        self._module.AIService._classify_task_complexity = self._orig


async def run_round(dataset: list[dict], user_id: str, variant: str, selected_ds_id: str) -> list[dict]:
    """跑一轮：设置注入开关并强制编排器后跑全部题目，返回每题压缩结果。"""
    from app.config import settings

    settings.AGENT_PLAN_INJECTION_ENABLED = variant == "on"
    log.info("=== round=%s plan_injection=%s ===", variant, settings.AGENT_PLAN_INJECTION_ENABLED)
    print(f"=== 开始 {variant} 轮：全局计划注入 = {settings.AGENT_PLAN_INJECTION_ENABLED}（强制编排器）===")

    rows: list[dict] = []
    for i, q in enumerate(dataset, 1):
        t0 = time.time()
        attempt = await run_agent(
            q, user_id=user_id, mode="orchestrator", entry="canvas",
            selected_datasource_id=selected_ds_id,
        )
        dur = round(time.time() - t0, 1)
        result = judge_attempt(q, attempt, sql_executor=None)
        rows.append({
            "question_id": q["id"],
            "query": q.get("query", ""),
            "error": attempt.error,
            "duration_s": dur,
            "iteration_count": attempt.iteration_count,
            "tool_seq": _tool_seq(attempt.events),
            "block_count": _block_count(attempt.events),
            "canvas_score": result.canvas_score,
            "canvas_chart_count_ok": result.canvas_chart_count_ok,
            "canvas_chart_types_ok": result.canvas_chart_types_ok,
            "canvas_narrative_present": result.canvas_narrative_present,
            "canvas_arrange_layout_ok": result.canvas_arrange_layout_ok,
            "canvas_block_order_ok": result.canvas_block_order_ok,
            "canvas_actual_chart_count": result.canvas_actual_chart_count,
            "canvas_actual_chart_types": result.canvas_actual_chart_types,
        })
        print(
            f"  [{i}/{len(dataset)}] {q['id']} score={'✅' if result.canvas_score else '❌'} "
            f"blocks={rows[-1]['block_count']} iter={attempt.iteration_count} {dur}s "
            f"tools={rows[-1]['tool_seq']}"
        )
    return rows


def _render_report(off_rows: list[dict], on_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# 画布「全局计划注入」AB 实验报告")
    lines.append("")
    lines.append(
        f"**生成时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}　"
        f"**题目数**：{len(off_rows)}　实验开关：AGENT_PLAN_INJECTION_ENABLED (off / on)"
    )
    lines.append("")
    lines.append("## 汇总")
    lines.append("")

    def _agg(rows: list[dict]) -> dict:
        if not rows:
            return {}
        n = len(rows)
        return {
            "pass": sum(1 for r in rows if r["canvas_score"]) / n,
            "chart_count": sum(1 for r in rows if r["canvas_chart_count_ok"]) / n,
            "chart_types": sum(1 for r in rows if r["canvas_chart_types_ok"]) / n,
            "narrative": sum(1 for r in rows if r["canvas_narrative_present"]) / n,
            "order": sum(1 for r in rows if r["canvas_block_order_ok"]) / n,
            "avg_blocks": sum(r["block_count"] for r in rows) / n,
            "avg_iter": sum(r["iteration_count"] for r in rows) / n,
            "avg_dur": sum(r["duration_s"] for r in rows) / n,
        }

    off_a, on_a = _agg(off_rows), _agg(on_rows)
    lines.append("| 指标 | off (无注入) | on (全局计划注入) |")
    lines.append("|---|---|---|")
    for key, label in [("pass", "画布总分通关率"), ("chart_count", "图表数达标率"), ("chart_types", "图表类型达标率"),
                       ("narrative", "叙事块达标率"), ("order", "块顺序达标率"), ("avg_blocks", "平均落块数"),
                       ("avg_iter", "平均迭代轮数"), ("avg_dur", "平均耗时(s)")]:
        lines.append(
            f"| {label} | {off_a.get(key, '-') if isinstance(off_a.get(key), str) else f'{off_a.get(key, 0):.2f}'} | "
            f"{on_a.get(key, '-') if isinstance(on_a.get(key), str) else f'{on_a.get(key, 0):.2f}'} |"
        )
    lines.append("")

    lines.append("## 题目级对照")
    lines.append("")
    lines.append("| 题号 | 问题 | 得分 off/on | 落块 off/on | 迭代 off/on | 耗时 off/on | 工具序列 off / on |")
    lines.append("|---|---|---|---|---|---|---|")
    by_id = {r["question_id"]: r for r in on_rows}
    for r in off_rows:
        qid = r["question_id"]
        o = by_id.get(qid, {})
        lines.append(
            f"| {qid} | {r['query'][:22]} | "
            f"{'✅' if r['canvas_score'] else '❌'}/{'✅' if o.get('canvas_score') else '❌'} | "
            f"{r['block_count']}/{o.get('block_count', '-')} | "
            f"{r['iteration_count']}/{o.get('iteration_count', '-')} | "
            f"{r['duration_s']}/{o.get('duration_s', '-')} | "
            f"`{' '.join(r['tool_seq'])}` / `{' '.join(o.get('tool_seq') or [])}` |"
        )
    lines.append("")
    lines.append("注：avg 均为均值；工具序列缩写：查Q=query_engine, 查SQL=query_sql, 文本块=add_text_block, 图表块=add_chart_block")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(exist_ok=True)
    dataset = load_dataset(Path(args.dataset))
    canvas = [q for q in dataset if q.get("category") == "canvas"]
    if not canvas:
        print("dataset 中无 category=canvas 题目")
        return 1
    if args.limit:
        canvas = canvas[: args.limit]
    print(f"画布题 {len(canvas)} 道，开始两轮实验（off → on），选中数据源将注入字段")
    selected_ds_id = await _pick_datasource_id(args.user_id)
    print(f"选中数据源: {selected_ds_id}")

    force = _ForceComplexClassifier()
    force.patch()
    try:
        off_rows = await run_round(canvas, args.user_id, "off", selected_ds_id)
        (OUT_DIR / "ab_off.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in off_rows), encoding="utf-8"
        )
        on_rows = await run_round(canvas, args.user_id, "on", selected_ds_id)
        (OUT_DIR / "ab_on.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in on_rows), encoding="utf-8"
        )
    finally:
        force.restore()

    report = _render_report(off_rows, on_rows)
    report_path = OUT_DIR / "ab_report.md"
    report_path.write_text(report, encoding="utf-8")
    print("\n=== 实验完成 ===")
    print(f"报告：{report_path}")
    print(f"原始：{OUT_DIR / 'ab_off.jsonl'} / {OUT_DIR / 'ab_on.jsonl'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="画布全局计划注入 AB 实验")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "dataset.jsonl"),
        help="评测数据集路径（默认全量，脚本只取 category=canvas）",
    )
    parser.add_argument("--user-id", default="21bee02f-dcb3-4108-b721-d8448db678e4", help="数据源归属用户")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 道画布题（0=全部）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())