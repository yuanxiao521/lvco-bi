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
    attempt.final_response = attempt.events[-2]["content"]
    attempt.iteration_count = 2

    return attempt


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