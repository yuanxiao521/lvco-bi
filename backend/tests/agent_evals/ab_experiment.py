"""主导 Agent（LeadAgent）AB 对照实验：同一套题，`LEAD_AGENT_ENABLED` 开/关各跑一轮真实执行。

对照逻辑（对齐 `lead-agent-upgrade-design.md` §7 阶段 5）：
- off 轮：`LEAD_AGENT_ENABLED=False` → 旧路径（复杂度分类器 + 编排器 / ReAct）。
- on  轮：`LEAD_AGENT_ENABLED=True`  → 主导 Agent 统一入口（意图 → 决策 → run_analysis）。

6 项指标：
- 报告完整率：是否产出非空 `report` / 最终回复。
- 落块数：`run_analysis` 返回的 `blocks_added`（off 轮用 canvas_action 计数近似）。
- 意图准确率：`intent` 事件是否命中期望意图（仅 on 轮有 `intent` 事件，off 轮记 N/A）。
- 平均步数：`run_analysis` 的 `steps`（off 轮用工具调用次数近似）。
- 平均耗时：单题墙钟耗时（秒）。
- 降级率：`done.degraded`（仅 on 轮有该信号，off 轮记 N/A）。

执行（需真实 LLM + 数据库 + 数据源）：
    cd backend && python tests/agent_evals/ab_experiment.py --user-id <uid>
    cd backend && python tests/agent_evals/ab_experiment.py --user-id <uid> --category all --limit 5

输出（tests/agent_evals/ab_output/）：
- ab_report_lead.md   题目级对照表 + 汇总
- ab_off.jsonl        开关关闭轮原始结果
- ab_on.jsonl         开关开启轮原始结果
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("ab_experiment")

from tests.agent_evals.run_eval import load_dataset, run_agent  # noqa: E402

# 输出目录：tests/agent_evals/ab_output/
OUT_DIR = Path(__file__).parent / "ab_output"

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
    "run_analysis": "分析",
}

# 期望意图映射：数据集全是"问数据 / 多步分析"类请求，可接受 data_qa / analysis；
# 画布题额外接受 canvas_edit（多图落块也可视为画布编辑）。
# 判错的典型是 chat / followup（把可执行请求误判为闲聊或追问）。
_EXPECTED_INTENT = {"canvas": {"canvas_edit", "data_qa", "analysis"}}
_DEFAULT_EXPECTED_INTENT = {"data_qa", "analysis"}


def _expected_intents(category: str) -> set[str]:
    return _EXPECTED_INTENT.get(category, _DEFAULT_EXPECTED_INTENT)


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None


def _tool_seq(events: list[dict]) -> list[str]:
    seq = []
    for e in events:
        if e.get("type") == "tool_call" and e.get("name"):
            seq.append(TOOL_LABEL.get(e["name"], e["name"]))
    return seq


def _block_count(events: list[dict]) -> int:
    """统计 canvas_action 落块数（旧路径 / Lead 透传事件的兜底口径）。"""
    cnt = 0
    for e in events:
        if e.get("type") == "tool_result":
            parsed = _loads(e.get("result"))
            if isinstance(parsed, dict) and isinstance(parsed.get("canvas_action"), dict):
                cnt += 1
    return cnt


def _lead_metrics(events: list[dict]) -> dict:
    """从 LeadAgent 事件流提取指标（intent / decision / run_analysis / done）。"""
    intent_ev = next((e for e in events if e.get("type") == "intent"), None)
    decision_ev = next((e for e in events if e.get("type") == "decision"), None)
    done_ev = next((e for e in reversed(events) if e.get("type") == "done"), None)
    report_evs = [
        e for e in events if e.get("type") == "report" and (e.get("content") or "").strip()
    ]

    ra_payload = None
    for e in events:
        if e.get("type") == "tool_result" and e.get("name") == "run_analysis":
            ra_payload = _loads(e.get("result"))

    if isinstance(ra_payload, dict):
        blocks = int(ra_payload.get("blocks_added") or 0)
        steps = int(ra_payload.get("steps") or 0)
    else:
        blocks = _block_count(events)
        totals = [int(e.get("total") or 0) for e in events if e.get("type") == "progress"]
        steps = max(totals) if totals else sum(1 for e in events if e.get("type") == "tool_call")

    return {
        "intent": (intent_ev or {}).get("intent"),
        "decision_action": (decision_ev or {}).get("action"),
        "report_ok": bool(report_evs),
        "blocks": blocks,
        "steps": steps,
        "degraded": bool((done_ev or {}).get("degraded")),
        "degradations": list((done_ev or {}).get("degradations") or []),
    }


def _legacy_metrics(events: list[dict], final_response: str) -> dict:
    """从旧路径事件流提取指标（无 intent / degraded 概念，置 None 表示 N/A）。"""
    report_ok = bool((final_response or "").strip()) or any(
        e.get("type") == "report" and (e.get("content") or "").strip() for e in events
    )
    return {
        "intent": None,
        "decision_action": None,
        "report_ok": report_ok,
        "blocks": _block_count(events),
        "steps": sum(1 for e in events if e.get("type") == "tool_call"),
        "degraded": None,
        "degradations": [],
    }


async def _pick_datasource_id(user_id: str) -> str:
    """取测试用户第一个数据源作为选中数据源（注入字段，接近真实体验）。"""
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
    """AB 实验强制走编排器（off 轮）：让旧路径稳定进入 Canvas/Agent Orchestrator。

    on 轮走 LeadAgent，不使用 `AIService._classify_task_complexity`，此 patch 对其无影响。
    """

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
    """跑一轮：设置 `LEAD_AGENT_ENABLED` 后跑全部题目，返回每题指标。"""
    from app.config import settings

    settings.LEAD_AGENT_ENABLED = variant == "on"
    log.info("=== round=%s lead_agent=%s ===", variant, settings.LEAD_AGENT_ENABLED)
    print(f"=== 开始 {variant} 轮：LEAD_AGENT_ENABLED = {settings.LEAD_AGENT_ENABLED} ===")

    rows: list[dict] = []
    for i, q in enumerate(dataset, 1):
        entry = "canvas" if q.get("category") == "canvas" else "chat"
        t0 = time.time()
        attempt = await run_agent(
            q, user_id=user_id, mode="real", entry=entry,
            selected_datasource_id=selected_ds_id,
        )
        dur = round(time.time() - t0, 1)

        if settings.LEAD_AGENT_ENABLED:
            m = _lead_metrics(attempt.events)
        else:
            m = _legacy_metrics(attempt.events, attempt.final_response)

        intent_ok: bool | None = None
        if m["intent"] is not None:
            intent_ok = m["intent"] in _expected_intents(q.get("category", ""))

        row = {
            "question_id": q["id"],
            "category": q.get("category", ""),
            "query": q.get("query", ""),
            "entry": entry,
            "error": attempt.error,
            "duration_s": dur,
            "intent": m["intent"],
            "intent_ok": intent_ok,
            "decision_action": m["decision_action"],
            "report_ok": m["report_ok"],
            "blocks": m["blocks"],
            "steps": m["steps"],
            "degraded": m["degraded"],
            "degradations": m["degradations"],
            "tool_seq": _tool_seq(attempt.events),
        }
        rows.append(row)
        print(
            f"  [{i}/{len(dataset)}] {q['id']}({row['category']}) "
            f"report={'✅' if row['report_ok'] else '❌'} "
            f"intent={row['intent'] or '-'} blocks={row['blocks']} steps={row['steps']} "
            f"degraded={row['degraded']} {dur}s"
        )
    return rows


def _rate(rows: list[dict], key: str) -> float | None:
    """对布尔列求比率，全为 None（该轮无此信号）时返回 None。"""
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v) / len(vals)


def _agg(rows: list[dict]) -> dict:
    if not rows:
        return {}
    n = len(rows)
    return {
        "report": _rate(rows, "report_ok"),
        "blocks": sum(r["blocks"] for r in rows) / n,
        "intent": _rate(rows, "intent_ok"),
        "steps": sum(r["steps"] for r in rows) / n,
        "dur": sum(r["duration_s"] for r in rows) / n,
        "degraded": _rate(rows, "degraded"),
    }


def _fmt_pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _fmt_num(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def _render_report(off_rows: list[dict], on_rows: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# 主导 Agent（LeadAgent）AB 对照实验报告")
    lines.append("")
    lines.append(
        f"**生成时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}　"
        f"**题目数**：{len(off_rows)}　实验开关：LEAD_AGENT_ENABLED (off / on)"
    )
    lines.append("")
    lines.append("## 6 项指标汇总")
    lines.append("")

    off_a, on_a = _agg(off_rows), _agg(on_rows)
    lines.append("| 指标 | off（旧路径） | on（主导 Agent） |")
    lines.append("|---|---|---|")
    lines.append(f"| 报告完整率 | {_fmt_pct(off_a.get('report'))} | {_fmt_pct(on_a.get('report'))} |")
    lines.append(f"| 落块数（平均） | {_fmt_num(off_a.get('blocks'))} | {_fmt_num(on_a.get('blocks'))} |")
    lines.append(f"| 意图准确率 | {_fmt_pct(off_a.get('intent'))} | {_fmt_pct(on_a.get('intent'))} |")
    lines.append(f"| 平均步数 | {_fmt_num(off_a.get('steps'))} | {_fmt_num(on_a.get('steps'))} |")
    lines.append(f"| 平均耗时(s) | {_fmt_num(off_a.get('dur'))} | {_fmt_num(on_a.get('dur'))} |")
    lines.append(f"| 降级率 | {_fmt_pct(off_a.get('degraded'))} | {_fmt_pct(on_a.get('degraded'))} |")
    lines.append("")
    lines.append("> 说明：`意图准确率` 与 `降级率` 依赖 LeadAgent 的 `intent` / `done.degraded` 事件，")
    lines.append("> 旧路径不产出该信号，故 off 列记 `-`（N/A）。")
    lines.append("")

    lines.append("## 题目级对照")
    lines.append("")
    lines.append(
        "| 题号 | 类别 | 问题 | 报告 off/on | 意图 off/on | 落块 off/on | "
        "步数 off/on | 耗时 off/on | 降级 off/on | 工具序列 off / on |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    by_id = {r["question_id"]: r for r in on_rows}
    for r in off_rows:
        o = by_id.get(r["question_id"], {})
        lines.append(
            f"| {r['question_id']} | {r['category']} | {r['query'][:20]} | "
            f"{'✅' if r['report_ok'] else '❌'}/{'✅' if o.get('report_ok') else '❌'} | "
            f"{r['intent'] or '-'}/{o.get('intent') or '-'} | "
            f"{r['blocks']}/{o.get('blocks', '-')} | "
            f"{r['steps']}/{o.get('steps', '-')} | "
            f"{r['duration_s']}/{o.get('duration_s', '-')} | "
            f"{r['degraded']}/{o.get('degraded', '-')} | "
            f"`{' '.join(r['tool_seq'])}` / `{' '.join(o.get('tool_seq') or [])}` |"
        )
    lines.append("")
    lines.append(
        "注：avg 均为均值；工具序列缩写：分析=run_analysis, 查Q=query_engine, 查SQL=query_sql, "
        "文本块=add_text_block, 图表块=add_chart_block。"
    )
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    OUT_DIR.mkdir(exist_ok=True)
    dataset = load_dataset(Path(args.dataset))
    if args.category == "all":
        subset = list(dataset)
    else:
        subset = [q for q in dataset if q.get("category") == args.category]
    if not subset:
        print(f"dataset 中无 category={args.category} 题目")
        return 1
    if args.limit:
        subset = subset[: args.limit]
    print(f"题目 {len(subset)} 道（category={args.category}），开始两轮实验（off → on）")
    selected_ds_id = await _pick_datasource_id(args.user_id)
    print(f"选中数据源: {selected_ds_id}")

    force = _ForceComplexClassifier()
    force.patch()
    try:
        off_rows = await run_round(subset, args.user_id, "off", selected_ds_id)
        (OUT_DIR / "ab_off.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in off_rows), encoding="utf-8"
        )
        on_rows = await run_round(subset, args.user_id, "on", selected_ds_id)
        (OUT_DIR / "ab_on.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in on_rows), encoding="utf-8"
        )
    finally:
        force.restore()

    report = _render_report(off_rows, on_rows)
    report_path = OUT_DIR / "ab_report_lead.md"
    report_path.write_text(report, encoding="utf-8")
    print("\n=== 实验完成 ===")
    print(f"报告：{report_path}")
    print(f"原始：{OUT_DIR / 'ab_off.jsonl'} / {OUT_DIR / 'ab_on.jsonl'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="主导 Agent（LeadAgent）AB 对照实验")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "dataset.jsonl"),
        help="评测数据集路径",
    )
    parser.add_argument("--user-id", default="21bee02f-dcb3-4108-b721-d8448db678e4", help="数据源归属用户")
    parser.add_argument(
        "--category",
        default="canvas",
        help="题目类别过滤：canvas（默认）| all（全量）| kpi/trend/... 任一类别",
    )
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 道题（0=全部）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
