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
from app.services.agents.planner_agent import _count_named_dimensions
from app.services.agents.tool_executor import is_error_result

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
    # 工具调用链摘要（供 Supervisor 决策参考：做了什么、成败、多少行）
    tool_chain: list[dict] = field(default_factory=list)


def make_idempotency_key(args: RunAnalysisArgs) -> str:
    """同目标 + 同数据源/画布 + 同入口 → 同一幂等键。"""
    raw = f"{args.goal}|{args.datasource_id}|{args.canvas_id}|{args.entry}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"run_analysis:{digest}"


def assess_subtask(result: RunAnalysisResult, *, entry: str = "chat", user_goal: str = "") -> str:
    """确定性评审：从子任务产物判定「是否达标」，返回一行带前缀的评估文本。

    不达标判定（任一命中即不达标，纯规则、零 LLM）：
      - 执行失败（success=False）
      - 报告数字未通过后验（verified=False）
      - 工具链中存在失败项（ok=False）
      - 画布入口但成功却零落块（该干活的没落到画布上）
      - 画布入口且用户点名了多个维度，但已落图表的维度覆盖不全（如点名
        品类/品牌/季节，只落了品类）——防止 Supervisor 看到"缺维度"却因
        "工具全成功"判达标而提前 stop（P001 实测踩坑）
    输出写入 turn_summaries（前缀 [子任务评估]），由决策器单独注入 prompt，
    促使 Lead 在"未达标"时带 guidance 重新派发，而不是盲目重复。
    """
    reasons: list[str] = []
    if not result.success:
        reasons.append(f"执行失败({result.error or 'unknown'})")
    if not result.verified:
        # 画布主交付是"块成功落盘"。若已产出内容块，叙事里"无法严格后验"的数字只属
        # 软存疑，不应把整体敲成失败触发重派级联；纯对话才把数字存疑当硬失败。
        if entry != "canvas" or result.blocks_added == 0:
            reasons.append("报告数字存疑")
    failed = [t.get("name") for t in result.tool_chain if not t.get("ok")]
    if failed:
        reasons.append("存在失败工具: " + ", ".join(str(n) for n in failed[:3]))
    if entry == "canvas" and result.success and result.blocks_added == 0:
        reasons.append("未在画布上产出内容块")
    # 维度覆盖检查：画布 + 用户点名 ≥2 维度 + 有图表落块记录
    if entry == "canvas" and result.success and result.blocks_added > 0:
        try:
            named_dims = _count_named_dimensions(user_goal or "")
        except Exception:  # noqa: BLE001
            named_dims = 0
        if named_dims >= 2:
            covered = _dimensions_covered(result.tool_chain)
            if covered < named_dims:
                reasons.append(f"维度覆盖不全（用户点名{named_dims}个维度，仅落{covered}个）")
    if reasons:
        return (
            "[子任务评估] 上轮未达标："
            + "；".join(reasons)
            + "。建议重新派发并附带改进指导意见，或 stop 后向用户说明情况。"
        )
    extra = "，画布已产出内容块" if result.blocks_added > 0 else ""
    return "[子任务评估] 上轮达标：工具调用全部成功，报告数字已验证" + extra + "。"


def _dimensions_covered(tool_chain: list[dict]) -> int:
    """从 execute 记录里统计画布图表实际用到的不同维度数（add/update 均计入）。

    解析 add_chart_block / update_chart_block 的 args 摘要（JSON 字符串），
    收集 dimensions 列表去重计数；解析失败的调用忽略（不影响总判定）。
    """
    covered: set[str] = set()
    for tc in tool_chain:
        name = str(tc.get("name") or "")
        if name not in ("add_chart_block", "update_chart_block"):
            continue
        args_s = tc.get("args") or ""
        try:
            args = json.loads(args_s)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        dims = args.get("dimensions")
        if isinstance(dims, list):
            for d in dims:
                if isinstance(d, str) and d.strip():
                    covered.add(d.strip().lower())
    return len(covered)


async def _load_canvas_snapshot(db_session, user_id, canvas_id) -> str:
    """读数据库里已落盘的画布块，渲染成布局摘要文本（供 Lead 决策 / Worker 注入）。

    数据源是 Canvas.blocks（前端是唯一写者，落块后保存到 DB），因此这是"已完成
    效果"的权威来源——比请求时刻上下文的快照更新。读取/解析任何异常都不抛出，
    返回空串（调用方按"无画布状态"处理）。
    """
    if not canvas_id or not user_id or db_session is None:
        return ""
    try:
        from uuid import UUID

        from app.repositories.canvas_repository import SQLAlchemyCanvasRepository
        from app.services.canvas_tools import render_canvas_layout

        canvas = await SQLAlchemyCanvasRepository(db_session).get_by_id(
            UUID(str(canvas_id)), UUID(str(user_id))
        )
        if canvas is None:
            return ""
        return render_canvas_layout(canvas.blocks, canvas_id=str(canvas.id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_tools] load_canvas_snapshot_failed error={e}")
        return ""


async def _load_canvas_narrative(db_session, user_id, canvas_id, max_chars: int = 96) -> str:
    """读画布已落盘的叙事文本块内容（排除 h1/h2 标题块），供 Lead 收尾带要点。

    画布场景结论落在 add_text_block 里，lead 收尾的模板句不含要点。本函数把
    非标题文本块的正文拼接成一句摘要，让收尾变成"已生成报告：<要点>（详见画布）"。
    无叙事或读取失败返回空串（调用方回退到原模板）。异常一律不抛出。
    """
    if not canvas_id or not user_id or db_session is None:
        return ""
    try:
        from uuid import UUID

        from app.repositories.canvas_repository import SQLAlchemyCanvasRepository
        from app.services.canvas_tools import _is_text_block

        canvas = await SQLAlchemyCanvasRepository(db_session).get_by_id(
            UUID(str(canvas_id)), UUID(str(user_id))
        )
        if canvas is None:
            return ""
        blocks = canvas.blocks if isinstance(canvas.blocks, list) else []
        parts: list[str] = []
        for b in blocks:
            if not _is_text_block(b):
                continue
            btype = str(b.get("blockType") or "")
            if btype in ("h1", "h2"):
                continue  # 标题不算叙事要点
            content = str(b.get("content") or (b.get("blocks") or [{}])[0].get("text", ""))
            content = " ".join(content.split()).strip()
            if content:
                parts.append(content)
        if not parts:
            return ""
        joined = "；".join(parts).replace("**", "")
        return joined if len(joined) <= max_chars else joined[:max_chars] + "…"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_tools] load_canvas_narrative_failed error={e}")
        return ""


async def _load_available_datasources(
    db_session, user_id: str, datasource_id: int | None,
    inject_fields_unselected: bool = False,
) -> list[dict]:
    """构建可用数据源列表（供 Planner 引用真实 id）。

    逻辑与 `ai_service.agent_stream` 保持一致：已选数据源 → 只注入该源且带字段；
    未选 → 默认只注入表级摘要（fields 置空），列名由 LLM 按需 list_fields 获取。
    但当 `inject_fields_unselected=True`（画布入口）时，未选也注入**首个数据源**的
    real 字段，让执行器在规划/落块前就拿到列名，避免裸猜（其余源仍保持表级摘要，
    防止一次性把所有源的全部字段塞爆上下文）。字段只在 plan 级注入一次，不随步骤重复。
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
        # 画布入口未选时，仅给首个数据源注入字段（默认目标源），其余源保持空字段摘要
        prime_field_id = None
        if not selected and inject_fields_unselected and datasources:
            prime_field_id = str(datasources[0].id)
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
                "fields": fields if (selected == str(ds.id) or str(ds.id) == prime_field_id) else [],
                "fields_injected": bool(selected) or str(ds.id) == prime_field_id,
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
    worker_guidance: str = "",
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

    # 画布感知（对 Worker）：把数据库中已落盘的画布布局注入执行上下文。
    # 不拼进 goal（保持幂等键稳定），以一条 assistant 历史消息携带——Worker 据此
    # 知道画布现状，避免重复添加；也可在落块后调用 get_canvas_layout 刷新/查重叠。
    if args.canvas_id:
        snapshot = await _load_canvas_snapshot(db_session, user_id, args.canvas_id)
        if snapshot:
            history = history + [{
                "role": "assistant",
                "content": (
                    "【系统注入：当前画布布局（已落盘）】\n"
                    f"{snapshot}\n"
                    "提示：需要获取实时最新布局时可调用 get_canvas_layout；"
                    "完成落块后若发现块重叠，可调用 arrange_layout 整理。"
                ),
            }]

    # Lead 指导意见（Supervisor → Worker）：Lead 认为上轮未达标/需改进时给出的指令，
    # 注入为最高优先级上下文，Planner 据此带着评判制定新计划，而不是盲目重排。
    if worker_guidance:
        history = history + [{
            "role": "assistant",
            "content": (
                "【Lead 指导意见（注意：必须优先遵循）】\n"
                f"{worker_guidance[:600]}\n"
            ),
        }]

    available_datasources = (
        available_datasources
        if available_datasources is not None
        else await _load_available_datasources(
            db_session, user_id, args.datasource_id,
            inject_fields_unselected=(args.entry == "canvas"),
        )
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
    # 工具调用链：tool_call 暂存 args，tool_result 落定 name/args/ok/rows
    tool_chain: list[dict] = []
    pending_calls: dict[str, dict] = {}  # tool_call_index -> {name, args}
    current_index = 0

    def _compact_args(args) -> str:
        """args 转一行紧凑摘要：保留字段名，截断长值，总长 ≤ 240。"""
        if args is None:
            return ""
        if isinstance(args, str):
            return args[:240]
        try:
            text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return ""
        return text[:240]

    def _rows_from_result(result: str) -> int | None:
        """从工具结果中提取行数：优先 rows 字段，否则猜数组长度。"""
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, dict):
            rows = parsed.get("rows")
            if isinstance(rows, (list, tuple)):
                return len(rows)
            data = parsed.get("data")
            if isinstance(data, (list, tuple)):
                return len(data)
            if parsed.get("error"):
                return None
        return None

    def _collect(ev: dict) -> None:
        nonlocal plan_steps, blocks_added, error, report_source, numeric_claims, tool_chain, pending_calls, current_index
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
        elif etype == "tool_call":
            name = str(ev.get("name") or "?")
            pending_calls[str(current_index)] = {"name": name, "args": _compact_args(ev.get("args"))}
            current_index += 1
        elif etype == "tool_result":
            blocks_added += _count_canvas_action(ev.get("result"))
            name = str(ev.get("name") or "?")
            result_str = ev.get("result")
            try:
                numeric_claims.update(_extract_numeric_claims(json.loads(result_str or "{}")))
            except (json.JSONDecodeError, TypeError):
                pass
            # 从最新一条 tool_call 取参数摘要（事件流里 call 必先于 result）
            args_s = ""
            for idx in sorted(pending_calls, key=lambda k: int(k)):
                if pending_calls[idx]["name"] == name:
                    args_s = pending_calls[idx].get("args", "")
                    del pending_calls[idx]
                    break
            rows = _rows_from_result(result_str) if isinstance(result_str, str) else None
            tool_chain.append({
                "name": name,
                "args": args_s,
                "ok": not is_error_result(result_str),
                "rows": rows,
            })
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

            is_canvas = args.entry == "canvas"
            if is_canvas:
                # 画布简单任务：react 也要"落块交付"。当前阶段工具已含 add_chart_block/
                # add_text_block，这里用一条系统指令固化落块契约，避免被当作纯文本问答。
                messages.append({
                    "role": "system",
                    "content": (
                        "任务环境：分析画布（Canvas）。你的最终交付是【在画布上落块】，不是纯文本回复。"
                        "规则：\n"
                        "1. 取数用 query_engine/query_sql，或直接 add_chart_block（它内部会自取数）。\n"
                        "2. 拿到数据后立即用 add_chart_block 生成图表块；字段名必须用字段清单里的真实列名（勿汉化/臆造）。\n"
                        "3. 需要文字时用 add_text_block 写叙事文本块；不要用 render_chart（那是对话框文本图，画布不用）。\n"
                        "4. 数据形态不合适就换维度/换图表类型重试，不要反复空查。"
                    ),
                })
            messages.append({"role": "user", "content": args.goal})

            queue: asyncio.Queue = asyncio.Queue()

            async def _emit(ev: dict) -> None:
                await queue.put(ev)

            async def _run() -> None:
                try:
                    react = ReactGraphAgent(llm, _build_react_tools(extra_plannable_tools),
                                    agent_trace=trace, is_canvas=is_canvas)
                    # 画布入口（含简单任务）强制 analyzing：该阶段工具集已含 add_text_block/
                    # add_chart_block 等落块工具；否则画布简单任务（无 datasource_id）会落入
                    # selecting 阶段只暴露 list_datasources，调不了落块工具。
                    initial_phase = "analyzing" if (args.datasource_id or args.entry == "canvas") else "selecting"
                    await react.run(
                        messages=messages,
                        user_id=user_id,
                        db_session=db_session,
                        initial_phase=initial_phase,
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
            # progress 事件只供 ActivityFeed 展示，不 emit text 到主气泡
            # （主气泡只保留最终总结，工具执行细节由工作台卡片承载）
            await emit_fn({
                "type": "progress",
                "index": p.index,
                "total": p.total,
                "title": p.title,
                "status": p.status,
                "note": p.note,
                "tool": p.tool,
            })
            # 仅失败时额外 emit 一条 text 到主气泡（错误提示）
            if p.status == "fail":
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
        tool_chain=tool_chain,
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
