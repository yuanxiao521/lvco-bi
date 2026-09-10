"""`run_analysis` 工具：主导 Agent 调度「编排器 / ReAct」的统一入口。

设计要点（对齐 `lead-agent-upgrade-design.md` §4.4）：
- 主导 Agent 不直接对用户说话，而是把「复杂分析」委托给确定性的编排器/ReAct。
- `emit` **透传**（绝不吞事件），同时**旁路**给 `perceive_stream` 产出 `StepProgress`。
- 幂等键 `sha1(goal + datasource_id + canvas_id + entry)`：同会话内重复调用直接复用。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from app.config import settings
from app.services.agents.lead.lead_perception import (
    StepProgress,
    perceive_stream,
    render_progress_text,
)

logger = logging.getLogger("lvco.lead.tools")

ProgressHandler = Callable[[StepProgress], Awaitable[None]]


@dataclass
class RunAnalysisArgs:
    """`run_analysis` 入参。"""

    goal: str                                   # 用户目标（自然语言）
    datasource_id: int | None = None
    canvas_id: int | None = None
    entry: str = "chat"                         # "chat" | "canvas"
    constraints: dict = field(default_factory=dict)  # {"mode": "orchestrator"|"react", ...}


@dataclass
class RunAnalysisResult:
    """`run_analysis` 出参。"""

    success: bool
    report: str = ""
    steps: list[dict] = field(default_factory=list)   # 复用 __steps__ 结构
    blocks_added: int = 0
    error: str | None = None
    report_source: str = "orchestrator"               # | "template" | "react"
    elapsed_ms: int = 0
    # 幻觉后验：报告数字与工具结果一致性校验结果（True=全部可溯源 / False=附了 ⚠️ 警示）
    verified: bool = True


def make_idempotency_key(args: RunAnalysisArgs) -> str:
    """同目标 + 同数据源/画布 + 同入口 → 同一幂等键。"""
    raw = f"{args.goal}|{args.datasource_id}|{args.canvas_id}|{args.entry}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"run_analysis:{digest}"


async def _load_available_datasources(
    db_session, user_id: str, datasource_id: int | None
) -> list[dict]:
    """构建可用数据源列表（供 Planner 引用真实 id）。

    逻辑与 `ai_service.agent_stream` 保持一致：已选数据源 → 只注入该源且带字段；
    未选 → 只注入表级摘要（fields 置空）。
    """
    available: list[dict] = []
    try:
        import uuid

        from app.repositories.datasource_repository import (
            SQLAlchemyDataSourceRepository,
        )
        from app.services.agent_tools import duckdb_client
        from app.models.datasource import SourceType

        ds_repo = SQLAlchemyDataSourceRepository(db_session)
        user_uuid = uuid.UUID(str(user_id))
        datasources, _ = await ds_repo.list_datasources(
            user_uuid, page=1, page_size=100, source_type=None, status=None, search=None
        )
        selected = str(datasource_id) if datasource_id is not None else None
        if selected:
            datasources = [d for d in datasources if str(d.id) == selected]
        for ds in datasources:
            schema_meta = ds.schema_meta or {}
            fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
            conn_cfg = dict(ds.connection_config) if ds.connection_config else {}
            schema_name = duckdb_client.get_schema_name(
                str(user_id), str(ds.id), ds.name, db_name=conn_cfg.get("db_name", "")
            )
            if ds.source_type in (SourceType.postgresql, SourceType.mysql):
                table_name = (
                    schema_meta.get("table_name", "data")
                    if isinstance(schema_meta, dict)
                    else "data"
                )
                table_ref = f'"{schema_name}".public."{table_name}"'
            else:
                table_ref = f'"{schema_name}"."data"'
            available.append({
                "id": str(ds.id),
                "name": ds.name,
                "description": ds.description,
                "type": ds.source_type.value if ds.source_type else "unknown",
                "fields": fields if selected else [],
                "fields_injected": bool(selected),
                "table_ref": table_ref,
            })
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_tools] load_datasources_failed error={e}")
    return available


def _build_react_tools(extra_plannable_tools: set[str] | None) -> list[dict]:
    """构造 ReAct 路径的可用工具集（与 ai_service 入口级过滤一致）。"""
    from app.services.agent_tools import ToolRegistry
    from app.services.canvas_tools import CANVAS_TOOL_NAMES

    all_tools = ToolRegistry.schemas()
    if extra_plannable_tools:
        allowed = set(extra_plannable_tools)
        return [
            t for t in all_tools
            if isinstance(t, dict)
            and isinstance(t.get("function"), dict)
            and t["function"].get("name") in allowed
        ]
    return [
        t for t in all_tools
        if isinstance(t, dict)
        and isinstance(t.get("function"), dict)
        and t["function"].get("name") not in CANVAS_TOOL_NAMES
    ]


async def _iter_queue(queue: asyncio.Queue) -> AsyncIterator[dict]:
    while True:
        ev = await queue.get()
        if ev is None:
            break
        yield ev


def _count_canvas_action(result_str) -> int:
    """工具结果 JSON 内嵌 canvas_action 视为一次落块。"""
    try:
        parsed = json.loads(result_str) if isinstance(result_str, str) else None
    except (json.JSONDecodeError, TypeError):
        return 0
    if isinstance(parsed, dict) and isinstance(parsed.get("canvas_action"), dict):
        return 1
    return 0


# ── 幻觉后验：报告数字与工具结果一致性校验（P0）────────────────────────
# 分层：C 程序化数值核对（零 LLM 成本）→ 仅对嫌疑数字 B LLM 复核 → 未证实的
# 在报告尾部附加 ⚠️ 警示（不改正文）。所有路径（orchestrator/react/canvas）的
# tool_result 事件都汇聚到 run_analysis 主循环提取，天然全路径复用。

_VERIFIER_SYSTEM = """你是数据校验器。下面是 AI 分析报告中的一句话，以及这次查询工具返回的数值清单。
请逐条判定报告引用的数字是否可信。判定标准：
- confirmed：该数字在查询结果中出现过（允许千分位/取整/单位换算差异）
- derived：该数字可以由结果中的数字计算得出（如百分比、同比、合计），请写出计算公式
- unverified：结果中没有该数字，也无法由结果推导，疑似编造
只输出严格 JSON：{"judgments": [{"number": "23.5", "verdict": "confirmed|derived|unverified", "formula": "可选"}]}
不要输出其它内容。"""

_REPORT_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?(?:%|万|亿)?")


def _extract_numeric_claims(result_obj) -> set[float]:
    """递归提取工具结果中的所有数值（作为可信数值集）。"""
    vals: set[float] = set()

    def walk(o) -> None:
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            vals.add(float(o))

    walk(result_obj)
    return vals


def _report_number_tokens(report: str) -> list[tuple[str, float]]:
    """从报告文本抽取数字 token → (原始串, 数值)（%→×1，万→×1e4，亿→×1e8）。"""
    out: list[tuple[str, float]] = []
    for m in _REPORT_NUM_RE.finditer(report or ""):
        raw = m.group()
        s = raw.replace(",", "")
        mult = 1.0
        if s.endswith("%"):
            s = s[:-1]
        elif s.endswith("万"):
            mult = 1e4
            s = s[:-1]
        elif s.endswith("亿"):
            mult = 1e8
            s = s[:-1]
        try:
            out.append((raw, float(s) * mult))
        except ValueError:
            continue
    return out


def _claim_matches(value: float, claims: set[float]) -> bool:
    """数字是否可信：等值 / 结果里的比例×100 当百分比 / 万亿缩放反向。"""
    if any(abs(c - value) < 1e-6 for c in claims):
        return True
    if any(abs(c * 100 - value) < 1e-4 for c in claims):  # 结果 0.2 ↔ 报告 20%
        return True
    if any(abs(c / 1e4 - value) < 1e-4 or abs(c / 1e8 - value) < 1e-4 for c in claims):
        return True
    return False


async def verify_report_numbers(report: str, claims: set[float], llm=None) -> tuple[bool, list[str]]:
    """C+B：核对报告数字与工具结果；返回 (verified, 未证实数字列表)。异常不抛出。"""
    suspects = [raw for raw, v in _report_number_tokens(report) if not _claim_matches(v, claims)]
    if not suspects:
        return True, []
    if llm is None:
        return False, suspects
    # B 层：仅对嫌疑数字做一次轻量 LLM 复核（通常 0 次触发）
    try:
        import asyncio as _asyncio
        prompt = (
            f"【报告原文】\n{report[:2000]}\n\n"
            f"【可疑数字】{', '.join(suspects[:10])}\n"
            f"【查询结果数值清单（前 300 个）】\n{', '.join(str(x) for x in sorted(claims)[:300])}"
        )
        content = await _asyncio.wait_for(
            llm.complete(
                [
                    {"role": "system", "content": _VERIFIER_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                enable_thinking=False,
                max_tokens=300,
            ),
            timeout=8.0,
        )
        import json as _json
        obj = _json.loads(content or "{}")
        unverified = [
            str(j.get("number"))
            for j in (obj.get("judgments") or [])
            if str(j.get("verdict")) == "unverified"
        ]
        return (not unverified), unverified
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_tools] verify_llm_failed={e} keep_suspects")
        return False, suspects


async def run_analysis(
    args: RunAnalysisArgs,
    *,
    db_session,
    emit,
    llm,
    trace=None,
    lead_ctx=None,
    extra_plannable_tools: set[str] | None = None,
    on_progress: ProgressHandler | None = None,
    memo: dict | None = None,
    available_datasources: list[dict] | None = None,
) -> RunAnalysisResult:
    """执行一次分析：按 `entry`/`constraints.mode` 委托编排器或 ReAct。

    Args:
        args: 分析入参。
        db_session: 数据库会话。
        emit: 原始事件透传回调（绝不被拦截）。
        llm: LLM 客户端（委托编排器/ReAct 使用）。
        trace: 可选观测 trace。
        lead_ctx: 主导 Agent 上下文（提供 user_id / history）。
        extra_plannable_tools: 入口级工具白名单（画布入口传入）。
        on_progress: 感知回调；不传则用 `emit` 直接吐 progress/text。
        memo: 幂等缓存（dict）；命中直接复用。
        available_datasources: 预加载的可用数据源（不传则内部加载）。
    """
    started = time.perf_counter()
    key = make_idempotency_key(args)
    if memo is not None and key in memo:
        cached = memo[key]
        logger.info(f"[lead_tools] memo_hit key={key}")
        return cached

    emit_fn = emit if emit is not None else _noop
    user_id = str(getattr(lead_ctx, "user_id", "") or "")
    history = list(getattr(lead_ctx, "turns", []) or [])
    mode = str(args.constraints.get("mode") or ("canvas" if args.entry == "canvas" else "orchestrator"))

    available_datasources = (
        available_datasources
        if available_datasources is not None
        else await _load_available_datasources(db_session, user_id, args.datasource_id)
    )

    span = trace.span(name="run_analysis", span_type="chain") if trace is not None else None
    if span is not None:
        span.input = {
            "goal": args.goal[:200],
            "entry": args.entry,
            "mode": mode,
            "datasource_id": args.datasource_id,
            "canvas_id": args.canvas_id,
        }

    report_parts: list[str] = []
    plan_steps: list[dict] = []
    blocks_added = 0
    error: str | None = None
    report_source = "orchestrator" if mode != "react" else "react"
    # 幻觉后验：收敛每个 tool_result 的数值作为可信集（全路径统一 hook 点）
    numeric_claims: set[float] = set()

    def _collect(ev: dict) -> None:
        nonlocal plan_steps, blocks_added, error, report_source, numeric_claims
        etype = ev.get("type")
        if etype == "plan":
            steps = (ev.get("plan") or {}).get("steps")
            if isinstance(steps, list):
                plan_steps = steps
        elif etype == "text":
            content = ev.get("content")
            if content:
                report_parts.append(str(content))
            if ev.get("report_source"):
                report_source = str(ev["report_source"])
        elif etype == "tool_result":
            blocks_added += _count_canvas_action(ev.get("result"))
            try:
                numeric_claims.update(_extract_numeric_claims(json.loads(ev.get("result") or "{}")))
            except (json.JSONDecodeError, TypeError):
                pass
        elif etype == "error":
            error = str(ev.get("message") or "执行异常")

    async def _source() -> AsyncIterator[dict]:
        if mode == "canvas":
            from app.services.agents.canvas_orchestrator import CanvasOrchestrator
            orchestrator = CanvasOrchestrator(llm, db_session, extra_plannable_tools)
            async for ev in orchestrator.execute_task(
                user_msg=args.goal,
                history=history,
                user_id=user_id,
                available_datasources=available_datasources,
            ):
                yield ev
        elif mode == "react":
            from app.services.agents.react_agent import ReactGraphAgent
            from app.services.ai_prompts import AGENT_SYSTEM
            from app.services.context_utils import compress_history

            messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM}]
            if getattr(lead_ctx, "history_summary", ""):
                messages.append({
                    "role": "assistant",
                    "content": f"【压缩摘要】【历史记忆】{lead_ctx.history_summary}",
                })
            for h in history[-20:]:
                if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                    messages.append({"role": h["role"], "content": str(h.get("content", ""))})
            messages = compress_history(messages, keep=60, max_chars=150000)
            messages.append({"role": "user", "content": args.goal})

            queue: asyncio.Queue = asyncio.Queue()

            async def _emit(ev: dict) -> None:
                await queue.put(ev)

            async def _run() -> None:
                try:
                    react = ReactGraphAgent(llm, _build_react_tools(extra_plannable_tools))
                    await react.run(
                        messages=messages,
                        user_id=user_id,
                        db_session=db_session,
                        initial_phase="analyzing" if args.datasource_id else "selecting",
                        emit=_emit,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"[lead_tools] react_failed: {e}")
                    await _emit({"type": "error", "message": f"分析执行异常: {e}"})
                finally:
                    await queue.put(None)

            task = asyncio.create_task(_run())
            async for ev in _iter_queue(queue):
                yield ev
            await task
        else:
            from app.services.agents import AgentOrchestrator
            orchestrator = AgentOrchestrator(llm, db_session, extra_plannable_tools)
            async for ev in orchestrator.execute_task(
                user_msg=args.goal,
                history=history,
                user_id=user_id,
                available_datasources=available_datasources,
            ):
                yield ev

    async def _progress_consumer(perceive_q: asyncio.Queue) -> None:
        async for p in perceive_stream(_iter_queue(perceive_q)):
            if on_progress is not None:
                await on_progress(p)
                continue
            await emit_fn({
                "type": "progress",
                "index": p.index,
                "total": p.total,
                "title": p.title,
                "status": p.status,
                "note": p.note,
                "tool": p.tool,
            })
            if p.status == "fail" or settings.LEAD_PROGRESS_VERBOSE:
                await emit_fn({"type": "text", "content": render_progress_text(p)})

    raw_q: asyncio.Queue = asyncio.Queue()
    perceive_q: asyncio.Queue = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for ev in _source():
                await raw_q.put(ev)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[lead_tools] source_failed mode={mode}: {e}")
            await raw_q.put({"type": "error", "message": f"分析执行异常: {e}"})
        finally:
            await raw_q.put(None)

    producer = asyncio.create_task(_producer())
    consumer = asyncio.create_task(_progress_consumer(perceive_q))

    while True:
        ev = await raw_q.get()
        if ev is None:
            break
        await emit_fn(ev)
        # react 内核已自带 progress 反馈（每工具 start→ok/fail），
        # 跳过感知旁路翻译，避免同一工具在前端出现两份进度条。
        if mode != "react":
            await perceive_q.put(ev)
        _collect(ev)
    await perceive_q.put(None)
    await consumer
    await producer

    report = "".join(report_parts).strip()
    success = error is None and bool(report)
    if not success and error is None:
        error = "empty_report"
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # 幻觉后验：报告数字与工具结果一致性核对（C 规则 → 嫌疑 B 复核 → ⚠️ 警示，不阻塞）
    verified, unverified_nums = True, []
    if report:
        try:
            verified, unverified_nums = await verify_report_numbers(report, numeric_claims, llm)
        except Exception:  # noqa: BLE001
            logger.warning("[lead_tools] verify_report failed, skip verification")
            verified, unverified_nums = True, []
        if unverified_nums:
            say_nums = ", ".join(unverified_nums[:8])
            report = f"{report}\n\n> ⚠️ 以下数据未在查询结果中出现，请人工核实：{say_nums}"

    result = RunAnalysisResult(
        success=success,
        report=report,
        steps=plan_steps,
        blocks_added=blocks_added,
        error=error,
        report_source=report_source,
        elapsed_ms=elapsed_ms,
        verified=verified,
    )
    if span is not None:
        span.update(output={
            "success": success,
            "report_chars": len(report),
            "steps": len(plan_steps),
            "blocks_added": blocks_added,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "verify_verified": verified,
            "verify_unverified": unverified_nums[:8],
        })
        span.finish()
    if memo is not None and success:
        memo[key] = result
    logger.info(
        f"[lead_tools] run_analysis done mode={mode} success={success} "
        f"steps={len(plan_steps)} blocks={blocks_added} chars={len(report)} elapsed_ms={elapsed_ms}"
    )
    return result


async def _noop(_ev: dict) -> None:
    return None
