# -*- coding: utf-8 -*-
"""agent_calls 时间线 CLI（P1-C）。

用法（先 cd backend，让 .env / app 包可被找到）：

    .venv/Scripts/python.exe scripts/agent_trace_cli.py --trace <trace_id>
    .venv/Scripts/python.exe scripts/agent_trace_cli.py --turn  <turn_id>
    .venv/Scripts/python.exe scripts/agent_trace_cli.py --trace <id> --errors-only --limit 200

输出：按 (seq, id) 排序的时间线表格 + 末尾汇总（LLM 调用数 / token 合计 / 工具失败数）。

注意：trace = 一次请求；turn = 一轮对话（HITL 确认续跑会跨多个 trace，用 turn 看全）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _short(value, limit: int = 60) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dur(ms) -> str:
    if ms is None:
        return "-"
    return f"{int(ms)}ms"


async def _load(trace_id: uuid.UUID | None, turn_id: uuid.UUID | None, limit: int, errors_only: bool):
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.models.agent_call import AgentCall

    async with async_session_factory() as db:
        stmt = select(AgentCall)
        if trace_id is not None:
            stmt = stmt.where(AgentCall.trace_id == trace_id)
        if turn_id is not None:
            stmt = stmt.where(AgentCall.turn_id == turn_id)
        if errors_only:
            stmt = stmt.where(AgentCall.ok.is_(False))
        stmt = stmt.order_by(AgentCall.seq, AgentCall.id).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())


def _print_timeline(rows: list) -> None:
    if not rows:
        print("(no rows)")
        return
    head = rows[0]
    print(
        f"trace={head.trace_id}  turn={head.turn_id}  entry={head.entry}  "
        f"session={head.session_id}  mode={head.mode}"
    )
    print("-" * 150)
    print(
        f"{'seq':>4}  {'kind':<8}  {'tool/name':<24}  {'ok':<3}  {'dur':>8}  "
        f"{'memo':<4}  {'error_kind':<18}  detail"
    )
    print("-" * 150)
    for row in rows:
        if row.kind == "llm":
            name = row.model or "llm"
            detail = f"in={row.tokens_in or 0} out={row.tokens_out or 0} cached={row.tokens_cached or 0}"
        elif row.kind == "turn":
            name = "(turn)"
            detail = (
                f"rounds={row.rounds} tools={row.tool_calls} fails={row.tool_failures} "
                f"degraded={row.degraded}"
            )
        else:
            name = row.tool_name or row.kind
            detail = row.error_msg or row.result_preview
        ok = "T" if row.ok is True else ("F" if row.ok is False else "-")
        memo = "hit" if row.memo_hit else ""
        print(
            f"{row.seq:>4}  {row.kind:<8}  {_short(name, 24):<24}  {ok:<3}  "
            f"{_dur(row.duration_ms):>8}  {memo:<4}  {_short(row.error_kind, 18):<18}  {_short(detail)}"
        )
    print("-" * 150)

    llm_rows = [r for r in rows if r.kind == "llm"]
    tool_rows = [r for r in rows if r.kind == "tool"]
    tool_fail = [r for r in tool_rows if r.ok is False]
    turn = next((r for r in rows if r.kind == "turn"), None)
    print(
        f"summary: llm_calls={len(llm_rows)} "
        f"tokens_in={sum(r.tokens_in or 0 for r in llm_rows)} "
        f"tokens_out={sum(r.tokens_out or 0 for r in llm_rows)} "
        f"cached={sum(r.tokens_cached or 0 for r in llm_rows)} | "
        f"tool_calls={len(tool_rows)} tool_failures={len(tool_fail)}"
    )
    if turn is not None:
        print(
            f"turn: rounds={turn.rounds} tool_calls={turn.tool_calls} "
            f"tool_failures={turn.tool_failures} degraded={turn.degraded} "
            f"duration_ms={turn.duration_ms}"
        )
    if tool_fail:
        kinds: dict[str, int] = {}
        for row in tool_fail:
            kinds[row.error_kind or "?"] = kinds.get(row.error_kind or "?", 0) + 1
        print("error_kind breakdown: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent_calls 时间线")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trace", help="按 trace_id（一次请求）查询")
    group.add_argument("--turn", help="按 turn_id（一轮对话）查询")
    parser.add_argument("--limit", type=int, default=500, help="最多返回行数（默认 500）")
    parser.add_argument("--errors-only", action="store_true", help="只看失败行（ok=false）")
    args = parser.parse_args(argv)

    try:
        trace_id = uuid.UUID(args.trace) if args.trace else None
        turn_id = uuid.UUID(args.turn) if args.turn else None
    except (ValueError, TypeError):
        print("trace/turn 必须是 UUID", file=sys.stderr)
        return 2

    rows = asyncio.run(_load(trace_id, turn_id, max(1, args.limit), args.errors_only))
    _print_timeline(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
