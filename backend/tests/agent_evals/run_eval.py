"""Agent 评测运行入口。

执行：
    cd backend && python -m tests.agent_evals.run_eval

或：
    cd backend && python tests/agent_evals/run_eval.py --dataset tests/agent_evals/dataset.jsonl --output tests/agent_evals/baseline_report.md

输出：
- 控制台打印实时进度
- 生成 markdown 报告（baseline_report.md）
- 每道题详细结果（results.jsonl）

环境要求：
- LLM 已配置（OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL）
- 数据库已初始化（含评测用 mock 数据）
- 单用户环境（自动创建/使用一个 demo 用户）

注意：完整跑通需真实 LLM 调用，建议先用 LANGFUSE_ENABLED=false 跑一次看通流程。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

# 让 backend/ 作为根目录
BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
# 开启 orchestrator 的 DEBUG 日志
logging.getLogger("lvco.agent.orchestrator").setLevel(logging.DEBUG)
log = logging.getLogger("lvco.agent_evals")

from tests.agent_evals.judge import (
    AttemptTrace,
    EvalResult,
    aggregate_results,
    judge_attempt,
)


# ----------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------


def load_dataset(path: Path) -> list[dict[str, Any]]:
    """加载 jsonl 数据集。"""
    items: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    log.info("loaded %d questions from %s", len(items), path)
    return items


# ----------------------------------------------------------------------
# Agent 执行（可被替换为 mock）
# ----------------------------------------------------------------------


async def run_agent(question: dict[str, Any], user_id: str = "21bee02f-dcb3-4108-b721-d8448db678e4", mode: str = "real") -> AttemptTrace:
    """运行一次 Agent，返回完整轨迹。

    默认使用真实 AIService，通过 --user-id 指定评测账号（数据源归属于该账号）。
    如要离线测试，可 patch 为 mock 实现。
    mode: "real" 单 Agent ReAct, "orchestrator" 多 Agent 编排模式
    """
    from app.core.database import async_session_factory
    from app.services.ai_service import AIService
    from app.services.llm_client import LLMClient

    attempt = AttemptTrace(
        question_id=question["id"],
        user_msg=question["query"],
    )

    # Orchestrator 模式需要 initial_phase="selecting" 且消息长度 > 20
    initial_phase = "selecting" if mode == "orchestrator" else "analyzing"
    # Orchestrator 模式要求消息长度 > 20，给问题加上前缀
    user_msg = question["query"]
    if mode == "orchestrator" and len(user_msg) <= 20:
        user_msg = f"请帮我详细分析以下问题：{user_msg}"

    try:
        ai = AIService(LLMClient())
        events: list[dict[str, Any]] = []
        # db_session 必须真实：list_datasources / query_datasource 都要查库
        async with async_session_factory() as db:
            async for ev in ai.agent_stream(
                user_id=user_id,
                user_msg=user_msg,
                history=[],
                db_session=db,
                initial_phase=initial_phase,
            ):
                events.append(ev)
                if ev.get("type") == "text":
                    attempt.final_response += ev.get("content", "")
                attempt.iteration_count = max(
                    attempt.iteration_count,
                    sum(1 for e in events if e.get("type") == "tool_call"),
                )
        attempt.events = events
    except Exception as e:
        attempt.error = str(e)
        log.warning("agent error q=%s err=%s", question["id"], e)

    return attempt


async def run_agent_mock(question: dict[str, Any]) -> AttemptTrace:
    """Mock Agent 执行（无需 LLM）。用于离线评分。"""
    attempt = AttemptTrace(
        question_id=question["id"],
        user_msg=question["query"],
    )

    # 画布题走 canvas_action 协议；非画布题走单图 render_chart 协议。
    if question.get("category") == "canvas":
        attempt.events = _build_mock_canvas_events(question)
    else:
        # 模拟一次成功执行：调用 query_datasource + render_chart
        attempt.events = [
            {"type": "tool_call", "name": "list_datasources", "args": {}},
            {"type": "tool_result", "name": "list_datasources", "result": '{"items": []}'},
            {
                "type": "tool_call",
                "name": "query_datasource",
                "args": {"sql": question.get("expected_sql_template", "SELECT 1")},
            },
            {"type": "tool_result", "name": "query_datasource", "result": '{"rows": []}'},
            {
                "type": "tool_call",
                "name": "render_chart",
                "args": {"chart_type": question.get("expected_chart_type", "bar")},
            },
            {
                "type": "chart",
                "chart_type": question.get("expected_chart_type", "bar"),
                "option": {},
            },
            {
                "type": "text",
                "content": f"分析结果：{question.get('expected_keywords', ['数据'])[0]} 已生成。",
            },
            {"type": "done"},
        ]
    attempt.final_response = _extract_mock_final_response(attempt.events, question)
    attempt.iteration_count = sum(1 for e in attempt.events if e.get("type") == "tool_call")

    return attempt


def _build_mock_canvas_events(question: dict[str, Any]) -> list[dict[str, Any]]:
    """为画布题构造符合 canvas_action 协议的 tool_result 序列。

    协议：add_text_block / add_chart_block / arrange_layout（最后一个出现以触发收尾）
    """
    expected_types: list[str] = list(question.get("expected_chart_types") or [])
    if not expected_types:
        # 退化：用 expected_chart_type 兜底
        expected_types = [question.get("expected_chart_type", "bar")]
    requires_narrative = bool(question.get("requires_narrative"))
    requires_arrange = bool(question.get("requires_arrange_layout"))

    events: list[dict[str, Any]] = [
        {"type": "tool_call", "name": "list_datasources", "args": {}},
        {"type": "tool_result", "name": "list_datasources", "result": '{"items": []}'},
    ]

    # 叙事先于图表（顺序正确，可过 block_order_ok）
    if requires_narrative:
        events.extend([
            {
                "type": "tool_call",
                "name": "add_text_block",
                "args": {"block_type": "h1", "content": "销售分析报告"},
            },
            {
                "type": "tool_result",
                "name": "add_text_block",
                "result": json.dumps({
                    "canvas_action": {
                        "action": "add_text_block",
                        "block": {"type": "h1", "content": "销售分析报告"},
                    }
                }),
            },
        ])

    # 多图表块
    for i, ct in enumerate(expected_types):
        events.extend([
            {
                "type": "tool_call",
                "name": "add_chart_block",
                "args": {"chart_type": ct, "title": f"{question['id']} 图表{i+1}"},
            },
            {
                "type": "tool_result",
                "name": "add_chart_block",
                "result": json.dumps({
                    "canvas_action": {
                        "action": "add_chart_block",
                        "block": {
                            "chartType": ct,
                            "title": f"{question['id']} 图表{i+1}",
                            "datasourceId": "ds-1",
                            "queryConfig": {"sql": "SELECT 1"},
                            "columns": [],
                            "rows": [],
                        },
                    }
                }),
            },
        ])

    # 收尾叙事（仅当要求 narrative，给块顺序留下缓冲）
    if requires_narrative:
        events.extend([
            {
                "type": "tool_call",
                "name": "add_text_block",
                "args": {"block_type": "text", "content": "综合结论：以上从趋势、占比、TOP 多角度展开。"},
            },
            {
                "type": "tool_result",
                "name": "add_text_block",
                "result": json.dumps({
                    "canvas_action": {
                        "action": "add_text_block",
                        "block": {
                            "type": "text",
                            "content": "综合结论：以上从趋势、占比、TOP 多角度展开。",
                        },
                    }
                }),
            },
        ])

    # arrange_layout 放在末尾 → 满足"末尾 1/3 窗口"
    if requires_arrange:
        events.extend([
            {"type": "tool_call", "name": "arrange_layout", "args": {"layout": "grid-2col"}},
            {
                "type": "tool_result",
                "name": "arrange_layout",
                "result": json.dumps({
                    "canvas_action": {
                        "action": "arrange_layout",
                        "layout": {"columns": 2, "gap": 16},
                    }
                }),
            },
        ])

    events.append({"type": "done"})
    return events


def _extract_mock_final_response(events: list[dict[str, Any]], question: dict[str, Any]) -> str:
    """Mock 模式下的最终回复：文本块或关键词兜底。"""
    for e in reversed(events):
        if e.get("type") == "text":
            return e.get("content", "")
        if e.get("type") == "tool_result":
            res = e.get("result")
            if isinstance(res, str):
                try:
                    parsed = json.loads(res)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    block = (parsed.get("canvas_action") or {}).get("block") or {}
                    if block.get("content"):
                        return str(block["content"])
    kws = question.get("expected_keywords") or ["数据"]
    return f"分析结果：{kws[0]} 已生成。"


def question_expected_types(q: dict[str, Any]) -> list[str]:
    """从 dataset 中提取期望的图表类型集合（兼容 expected_chart_types / expected_chart_type）。"""
    if q.get("expected_chart_types"):
        return [str(t) for t in q["expected_chart_types"]]
    if q.get("expected_chart_type"):
        return [str(q["expected_chart_type"])]
    return []


def build_sql_executor(attempt: AttemptTrace):
    """从 attempt 的 list_datasources 结果解析真实 table_ref，构造 SQL 执行器。

    评测数据集的 expected_sql_template 使用裸表名（如 ecommerce_orders），
    而 Agent 使用真实 DuckDB 表引用（"schema"."data"）。执行器把裸表名替换为
    真实引用，分别执行 expected_sql 与 agent_sql 并比较结果集，
    得到真实 SQL 准确率（替代关键字重叠的文本近似）。
    """
    table_ref = None
    for e in attempt.events:
        if e.get("type") == "tool_result" and e.get("name") == "list_datasources":
            try:
                parsed = json.loads(e.get("result") or "{}")
            except Exception:
                continue
            for it in parsed.get("datasources") or []:
                if isinstance(it, dict) and it.get("table_ref"):
                    table_ref = it["table_ref"]
                    break
            if table_ref:
                break
    if not table_ref:
        return None

    def exec_sql(sql: str) -> list:
        if not sql:
            return []
        from app.core.duckdb_client import duckdb_client

        final = re.sub(r"\becommerce_orders\b", table_ref, sql)
        rows = duckdb_client.fetchall(final)
        return [list(r) for r in rows]

    return exec_sql


# ----------------------------------------------------------------------
# 报告生成
# ----------------------------------------------------------------------


def render_markdown_report(
    summary: dict[str, Any],
    results: list[EvalResult],
    dataset: list[dict[str, Any]],
    duration_s: float,
) -> str:
    """生成 markdown 评测报告。"""
    lines: list[str] = []
    lines.append("# Agent 评测 Baseline 报告")
    lines.append("")
    lines.append(f"**生成时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**数据集**：`dataset.jsonl`（{summary['total']} 道题）")
    lines.append(f"**总耗时**：{duration_s:.1f}s")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 | 通过率 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| SQL 准确率 | {summary['sql_accuracy_count']} / {summary['total']} | "
        f"**{summary['sql_accuracy_rate'] * 100:.1f}%** |"
    )
    lines.append(
        f"| 工具调用成功率 | {summary['tool_success_count']} / {summary['total']} | "
        f"**{summary['tool_success_rate'] * 100:.1f}%** |"
    )
    lines.append(
        f"| 输出有效率 | {summary['final_response_valid_count']} / {summary['total']} | "
        f"**{summary['final_response_valid_rate'] * 100:.1f}%** |"
    )
    lines.append(
        f"| 图表类型正确率 | {summary['chart_type_correct_count']} / {summary['total']} | "
        f"**{summary['chart_type_correct_rate'] * 100:.1f}%** |"
    )
    lines.append(
        f"| 图表配置合法率 | {summary['chart_config_valid_count']} / {summary['total']} | "
        f"**{summary['chart_config_valid_rate'] * 100:.1f}%** |"
    )
    lines.append(f"| 平均迭代轮数 | {summary['avg_iterations']} | - |")
    lines.append("")

    lines.append("## 分类分布")
    lines.append("")
    cat_stats: dict[str, list[EvalResult]] = {}
    for r in results:
        q = next((q for q in dataset if q["id"] == r.question_id), {})
        cat = q.get("category", "other")
        cat_stats.setdefault(cat, []).append(r)

    lines.append("| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |")
    lines.append("|---|---|---|---|---|")
    for cat, rs in sorted(cat_stats.items()):
        n = len(rs)
        sql = sum(1 for r in rs if r.sql_accuracy)
        tool = sum(1 for r in rs if r.tool_success)
        resp = sum(1 for r in rs if r.final_response_valid)
        lines.append(f"| {cat} | {n} | {sql} | {tool} | {resp} |")
    lines.append("")

    # ---- 画布专项章节 ----
    if summary.get("canvas_total", 0) > 0:
        lines.append("## 画布专项")
        lines.append("")
        lines.append(f"画布题共 **{summary['canvas_total']}** 道，覆盖多图表 / 叙事 / 布局三维度。")
        lines.append("")
        lines.append("| 子项 | 含义 | 通过率 |")
        lines.append("|---|---|---|")
        lines.append(
            f"| canvas_score | 5 子项全部通过 | **{summary['canvas_score_rate'] * 100:.1f}%** |"
        )
        lines.append(
            f"| chart_count | 实际图表数 ≥ 期望最小图表数 | {summary['canvas_chart_count_rate'] * 100:.1f}% |"
        )
        lines.append(
            f"| chart_types | 实际图表类型 ≥ 50% 覆盖期望集合 | {summary['canvas_chart_types_rate'] * 100:.1f}% |"
        )
        lines.append(
            f"| narrative | 含 h1/h2/text 叙事块 | {summary['canvas_narrative_rate'] * 100:.1f}% |"
        )
        lines.append(
            f"| arrange_layout | arrange_layout 出现在末尾 1/3 窗口 | {summary['canvas_arrange_layout_rate'] * 100:.1f}% |"
        )
        lines.append(
            f"| block_order | 叙事块先于最早图表块 | {summary['canvas_block_order_rate'] * 100:.1f}% |"
        )
        lines.append("")

        # 画布题逐题明细（仅在数量适中时展示）
        canvas_results = [r for r in results if r.is_canvas_question]
        if canvas_results:
            lines.append("### 画布题明细")
            lines.append("")
            lines.append("| 题目 | 期望类型 | 实际类型 | 图表数 | 通过子项 | 总分 |")
            lines.append("|---|---|---|---|---|---|")
            for r in canvas_results:
                q = next((q for q in dataset if q["id"] == r.question_id), {})
                exp = ",".join(question_expected_types(q))
                act = ",".join(r.canvas_actual_chart_types) or "-"
                subs = (
                    ("count✓ " if r.canvas_chart_count_ok else "count✗ ")
                    + ("types✓ " if r.canvas_chart_types_ok else "types✗ ")
                    + ("narr✓ " if r.canvas_narrative_present else "narr✗ ")
                    + ("arr✓ " if r.canvas_arrange_layout_ok else "arr✗ ")
                    + ("order✓" if r.canvas_block_order_ok else "order✗")
                )
                lines.append(
                    f"| {r.question_id} | {exp or '-'} | {act} | {r.canvas_actual_chart_count} | {subs} | "
                    f"{'✅' if r.canvas_score else '❌'} |"
                )
            lines.append("")

    lines.append("## 失败案例（top 10）")
    lines.append("")
    failed = [
        r for r in results
        if not (r.sql_accuracy and r.tool_success and r.final_response_valid and r.chart_config_valid)
    ]
    for r in failed[:10]:
        q = next((q for q in dataset if q["id"] == r.question_id), {})
        lines.append(f"### {r.question_id} - {q.get('query', '?')}")
        lines.append("")
        lines.append(f"- 类别：`{q.get('category', '')}`")
        lines.append(f"- SQL 准确：{'✅' if r.sql_accuracy else '❌'}")
        lines.append(f"- 工具成功：{'✅' if r.tool_success else '❌'}")
        lines.append(f"- 输出有效：{'✅' if r.final_response_valid else '❌'}")
        lines.append(f"- 图表类型：`{r.actual_chart_type}` (期望 `{r.expected_chart_type}`)")
        if r.notes:
            lines.append(f"- 备注：`{'; '.join(r.notes)}`")
        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset)
    output_path = Path(args.output)
    results_path = Path(args.results)

    dataset = load_dataset(dataset_path)

    log.info("starting eval mode=%s", args.mode)
    start = time.time()
    results: list[EvalResult] = []
    attempts_log: list[dict[str, Any]] = []  # 事件存档（离线重放/复盘用）

    for i, q in enumerate(dataset, 1):
        log.info("[%d/%d] running q=%s", i, len(dataset), q["id"])
        if args.mode == "mock":
            attempt = await run_agent_mock(q)
        else:
            attempt = await run_agent(q, user_id=args.user_id, mode=args.mode)
        executor = build_sql_executor(attempt) if args.mode in ("real", "orchestrator") else None
        result = judge_attempt(q, attempt, sql_executor=executor)
        results.append(result)
        attempts_log.append({
            "question_id": q["id"],
            "query": q.get("query", ""),
            "error": attempt.error,
            "iteration_count": attempt.iteration_count,
            "events": attempt.events,
        })
        log.info(
            "[%d/%d] q=%s sql=%s tool=%s resp=%s chart_cfg=%s iter=%d",
            i, len(dataset), q["id"],
            "✓" if result.sql_accuracy else "✗",
            "✓" if result.tool_success else "✗",
            "✓" if result.final_response_valid else "✗",
            "✓" if result.chart_config_valid else "✗",
            result.iteration_count,
        )

    duration = time.time() - start
    summary = aggregate_results(results)

    log.info("=" * 60)
    log.info("EVAL DONE duration=%.1fs summary=%s", duration, json.dumps(summary, ensure_ascii=False))

    # 写报告
    md = render_markdown_report(summary, results, dataset, duration)
    output_path.write_text(md, encoding="utf-8")
    log.info("report written to %s", output_path)

    # 写原始结果
    with open(results_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
    log.info("raw results written to %s", results_path)

    # 写事件存档（供离线重放 judge / 复盘失败案例）
    events_path = Path(str(results_path).replace(".jsonl", "_events.jsonl"))
    with open(events_path, "w", encoding="utf-8") as f:
        for a in attempts_log:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    log.info("attempt events written to %s", events_path)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 评测运行器")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "dataset.jsonl"),
        help="评测数据集路径",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).parent / "baseline_report.md"),
        help="报告输出路径",
    )
    parser.add_argument(
        "--results",
        default=str(Path(__file__).parent / "results.jsonl"),
        help="每题原始结果输出",
    )
    parser.add_argument(
        "--mode",
        choices=["real", "mock", "orchestrator"],
        default="mock",
        help="real=单 Agent ReAct；orchestrator=多 Agent 编排模式；mock=本地模拟（无需 API）",
    )
    parser.add_argument(
        "--user-id",
        default="21bee02f-dcb3-4108-b721-d8448db678e4",
        help="real 模式下 Agent 使用的用户 ID（数据源归属于该账号）",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())