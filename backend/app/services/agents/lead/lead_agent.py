"""主导 Agent（LeadAgent）：唯一对用户负责的调度者。

一次对话的完整闭环（对齐 `lead-agent-upgrade-design.md` §4.5）：

    intent → decision → 分支执行 → progress 汇报 → report → memory 回流 → done

设计原则：
- **B 主导 + 确定性兜底**：LLM 只做「意图识别」与「决策」，执行一律委托确定性编排器。
- **绝不吞事件**：`run_analysis` 的原始事件全部透传，感知只做旁路翻译。
- **异常不抛出**：任何一步失败都降级为确定性行为，保证用户始终有输出。
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.config import settings
from app.services.context_utils import estimate_tokens
from app.services.agents.lead.lead_decider import (
    ActionType,
    Decision,
    decide_action,
    decide_action_merged,
)
from app.services.agents.lead.lead_perception import StepProgress, render_progress_text
from app.services.agents.lead.lead_tools import (
    RunAnalysisArgs,
    RunAnalysisResult,
    _load_available_datasources,
    _load_canvas_narrative,
    _load_canvas_snapshot,
    assess_subtask,
    run_analysis,
)

logger = logging.getLogger("lvco.lead.agent")

_ANSWER_SYSTEM = (
    "你是 LvcoBI 的数据智能助手。用简洁、专业的中文回答用户问题。"
    "如果问题与数据/分析相关但缺少必要信息（数据源、指标、时间范围），"
    "请礼貌地说明需要哪些信息，并给出下一步建议。不要编造数据。"
    "回答中涉及业务指标（销售额/订单量/客单价等，见上下文中的受治理指标清单）"
    "时必须引用清单里的指标 key 与名称，不要把数据源裸字段（如 total_amount）当作指标名。"
)

_MEMORY_MERGE_TEMPLATE = (
    "把【已有长期记忆】与【新增对话】合并成一份**结构化长期记忆**，分四节输出，"
    "总量不超过 {max_chars} 字：\n"
    "【口径与规则】用户确立的统计口径、计算定义、筛选条件与偏好。**必须逐条保留、不得丢失**——"
    "口径被丢掉会让后续所有分析算错。\n"
    "【关键数字与结论】对话中出现的关键指标数值，**必须带时间范围/口径/来源**，"
    "例如「华东销售额 8,640 万（含税口径，2026Q3，Ecommerce Orders）」；"
    "只写「增长明显」这类没有数值也没有范围的表述没有价值，不要写。\n"
    "【数据源与字段】涉及的数据源、表与关键字段。\n"
    "【未决问题】尚未解决或待用户确认的事项。\n\n"
    "要求：\n"
    "1) 先在 <analysis>...</analysis> 里列草稿（逐条比对旧记忆与新增对话，标出重复与被推翻的条目），"
    "再在 <summary>...</summary> 里输出最终记忆；\n"
    "2) 旧记忆里的条目**逐条保留**，只删除「被新增对话明确推翻」或「完全重复」的——"
    "宁多勿丢，但同一事实不要写两遍；\n"
    "3) 某一节没有内容就写「（无）」；\n"
    "4) 除 <analysis> 与 <summary> 两个标签外，不要输出其他内容。"
)


def _memory_merge_system_prompt() -> str:
    """记忆合并的系统提示词（摘要字数上限来自配置）。"""
    max_chars = max(200, int(getattr(settings, "LEAD_MEMORY_SUMMARY_CHARS", 600) or 600))
    return _MEMORY_MERGE_TEMPLATE.format(max_chars=max_chars)

# 先打草稿再给摘要可显著提升合并质量，草稿由这里剥掉（不会进长期记忆）
_SUMMARY_TAG_RE = re.compile(r"<summary>(.*?)</summary>", re.S)


def _extract_summary(text: str) -> str:
    """取 <summary> 段内容；未按格式输出时退回整体文本。"""
    raw = (text or "").strip()
    m = _SUMMARY_TAG_RE.search(raw)
    return (m.group(1) if m else raw).strip()


def _resolve_memory_progress(total_rounds: int, covered_rounds: int) -> int:
    """归一化记忆合并进度：越界/脏数据（旧语义混杂、负数）一律按 0 处理。

    covered_rounds 契约：已并入长期记忆的用户轮数（累计、单调递增）。
    历史存量里可能混有旧语义（消息条数等），越界时归零——最多多合并一次，不丢内容。
    """
    if total_rounds <= 0:
        return 0
    if 0 <= covered_rounds <= total_rounds:
        return covered_rounds
    return 0


def _should_merge_memory(total_rounds: int, covered_rounds: int, min_rounds: int) -> bool:
    """节流判定：未合并的用户轮数达到 min_rounds 才触发一次记忆合并。"""
    if total_rounds <= 0 or min_rounds <= 0:
        return False
    return (total_rounds - _resolve_memory_progress(total_rounds, covered_rounds)) >= min_rounds


def _build_memory_merge_input(
    old_summary: str,
    new_messages: list[dict],
    per_msg_limit: int = 800,
    total_limit: int = 6000,
) -> str:
    """组装记忆合并输入：【已有长期记忆】+【新增对话】（逐条截断 + 总量封顶）。"""
    parts: list[str] = []
    if (old_summary or "").strip():
        parts.append(f"【已有长期记忆】\n{old_summary.strip()}")
    lines: list[str] = []
    for m in new_messages or []:
        content = str(m.get("content", "") or "").strip()
        if not content:
            continue
        role = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{role}: {content[:per_msg_limit]}")
    if lines:
        new_text = "\n".join(lines)[:total_limit]
        parts.append(f"【新增对话】\n{new_text}")
    return "\n\n".join(parts)


async def _count_session_user_rounds(db_session, session_id: str) -> int:
    """统计会话累计用户轮数（记忆合并进度的分母）；无 db/session 或查询失败返回 0。"""
    if db_session is None or not session_id:
        return 0
    try:
        from uuid import UUID

        from sqlalchemy import func, select

        from app.models.ai_message import AIMessage, AIMessageRole

        sid = session_id
        if isinstance(sid, str):
            sid = UUID(sid)
        result = await db_session.execute(
            select(func.count())
            .select_from(AIMessage)
            .where(AIMessage.session_id == sid, AIMessage.role == AIMessageRole.user)
        )
        return int(result.scalar() or 0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_agent] count_user_rounds_failed: {e}")
        return 0


async def _load_unmerged_messages(
    db_session,
    session_id: str,
    after_id: str | None = None,
    max_messages: int = 24,
) -> tuple[list[dict], str | None]:
    """取"水位之后最早的一段"未并入记忆的消息。

    返回 `(可并入的消息列表, 本段最后一条消息 id)`：
    - 消息列表已跳过空内容行（空占位不参与摘要，但**会**被水位跨越）；
    - 与旧实现的关键差别：旧实现取"最近 N 条"，积压超过窗口时中间段永远轮不到、
      却被进度标记成已并入；这里取"水位之后最早 N 条"，逐轮把水位向后推，不留空洞；
    - 水位指向的消息若已被删除（画布"新对话"会清空该会话历史），退回从头取。
    """
    if db_session is None or not session_id or max_messages <= 0:
        return [], None
    try:
        from uuid import UUID

        from sqlalchemy import and_, or_, select

        from app.models.ai_message import AIMessage

        sid = UUID(session_id) if isinstance(session_id, str) else session_id
        stmt = select(AIMessage).where(AIMessage.session_id == sid)
        if after_id:
            try:
                wm_uuid = UUID(str(after_id))
            except (ValueError, TypeError):
                wm_uuid = None
            cursor = None
            if wm_uuid is not None:
                cursor = (
                    await db_session.execute(
                        select(AIMessage).where(AIMessage.id == wm_uuid)
                    )
                ).scalar_one_or_none()
            if cursor is None:
                logger.info("[lead_agent] memory_watermark_missing: 退回从头取最早未并入段")
            else:
                # (created_at, role) 严格大于水位：同一轮的 user/assistant 时间戳相同
                # （事务级 now()），靠 role 的枚举序 user<assistant 区分 —— 既不会把已并入的
                # 行重取一遍，也不会漏掉同一轮稍后落库的助手回复。
                stmt = stmt.where(
                    or_(
                        AIMessage.created_at > cursor.created_at,
                        and_(
                            AIMessage.created_at == cursor.created_at,
                            AIMessage.role > cursor.role,
                        ),
                    )
                )
        rows = (
            await db_session.execute(
                stmt.order_by(AIMessage.created_at.asc(), AIMessage.role.asc()).limit(max_messages)
            )
        ).scalars().all()
        if not rows:
            return [], None
        kept = [
            {"id": str(r.id), "role": r.role.value, "content": str(r.content or "")}
            for r in rows
            if str(r.content or "").strip()
        ]
        return kept, str(rows[-1].id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[lead_agent] load_unmerged_messages_failed: {e}")
        return [], None


@dataclass
class LeadContext:
    """主导 Agent 的会话上下文（短期活记忆 + 长期摘要 + 汇报累积）。"""

    user_id: int | str
    session_id: str = ""
    entry: str = "chat"                       # "chat" | "canvas"
    canvas_id: int | None = None
    datasource_id: int | None = None
    history_summary: str = ""                 # 长期记忆（ai_memories 摘要）
    memory_covered: int = 0                   # 已并入长期记忆的用户轮数（节流用；内容水位见下）
    memory_watermark: str | None = None       # 记忆合并水位：已并入的最后一条消息 id
    memory_fail_count: int = 0                # 连续合并失败次数（熔断依据）
    turns: list[dict] = field(default_factory=list)          # 会话活记忆
    extra_context: str = ""                   # 额外上下文（如"上一轮已生成的图表"摘要），置于 digest 头部
    canvas_state: object | None = None        # 请求级画布状态提供者（CanvasStateProvider，stream 开头构造）
    turn_summaries: list[str] = field(default_factory=list)  # 关键节点汇报累积
    metrics_ctx: str = ""                     # 受治理指标清单（入口注入，answer/决策复用）

    def add_turn(self, role: str, content: str) -> None:
        """追加一轮对话（自动裁剪窗口，避免上下文膨胀）。"""
        if content is None:
            return
        self.turns.append({"role": role, "content": str(content)})
        cap = max(1, settings.LEAD_MAX_TURNS_IN_CTX) * 2
        if len(self.turns) > cap:
            self.turns = self.turns[len(self.turns) - cap:]

    def recent_turns(self, max_turns: int = 8, max_chars: int | None = None) -> list[dict]:
        """取最近若干**整条**消息（成对对齐 + 预算封顶），供答案生成这类轻量调用使用。

        与 digest 的分工：digest 把记忆与轮次拼成"一段文本"给子任务看；
        这里返回结构化消息列表，但仍遵守同样三条规则——不切句子、丢掉孤立回答开场、
        总字符预算，避免"最近 8 条"在长报告场景把 prompt 撑到上万字符。
        """
        limit = int(
            max_chars or getattr(settings, "LEAD_CTX_ANSWER_HISTORY_CHARS", 2000) or 2000
        )
        picked: list[dict] = []
        used = 0
        for t in reversed(self.turns[-max(1, max_turns):]):
            role = t.get("role")
            if role not in ("user", "assistant"):
                continue
            content = str(t.get("content", ""))
            if used + len(content) > limit:
                if not picked and limit - used > 40:
                    picked.append({
                        "role": role,
                        "content": content[: limit - used - 1] + "…",
                    })
                break
            picked.append({"role": role, "content": content})
            used += len(content)
        picked.reverse()
        # 丢掉开头的孤立回答；但若整个窗口只剩回答（提问都被预算裁掉），保留它——
        # 至少给模型一点"刚才在聊什么"的线索，比空手强。
        if any(t["role"] == "user" for t in picked):
            while picked and picked[0]["role"] != "user":
                picked.pop(0)
        return picked

    def digest(self, max_chars: int | None = None) -> str:
        """给子任务/决策注入的紧凑上下文（额外上下文 + 长期摘要 + 最近轮次）。

        装配规则（由"尾部硬切"改为"整条消息装配"）：
        - **不切句子**：活记忆按"整条消息"从新到旧累加，装不下就停；只有单条自身就超预算时
          才截断该条（旧实现是从整体尾部一刀切，会把一条消息切掉半个句子）；
        - **长期记忆优先但有上限**：先给【长期记忆】，但为其设上限 `预算 − 活记忆预留`，
          避免摘要变长时把"刚才这几轮"整个挤掉；反过来也不会让旧记忆把活记忆饿死；
        - **成对对齐**：裁剪后若开头是孤立 assistant（没有提问的回答），丢掉它——
          这种开场既占预算又容易误导（见 context_utils.align_history_pairs）。
        """
        limit = int(max_chars or getattr(settings, "LEAD_CTX_MAX_CHARS", 4000) or 4000)
        reserve = min(
            max(0, int(getattr(settings, "LEAD_CTX_LIVE_RESERVE_CHARS", 800) or 0)),
            limit // 2,
        )

        extra = (self.extra_context or "").strip()
        if extra:
            extra = extra[: max(0, limit // 4)]

        memory = ""
        if self.history_summary.strip():
            memory = f"【长期记忆】{self.history_summary.strip()}"
            max_memory = limit - reserve
            if 0 < max_memory < len(memory):
                memory = memory[:max_memory] + "…"

        turns = [
            {"role": t.get("role"), "content": str(t.get("content", ""))}
            for t in self.turns[-max(1, settings.LEAD_MAX_TURNS_IN_CTX):]
        ]
        used = len(memory) + len(extra)
        picked: list[str] = []
        for t in reversed(turns):
            line = f"{'用户' if t.get('role') == 'user' else '助手'}: {t['content']}"
            if used + len(line) + 1 > limit:
                remain = limit - used - 1
                if remain > 40 and not picked:
                    # 最新的这一条自身就超预算：截这一条，至少保住"刚才聊到哪"
                    picked.append(line[:remain] + "…")
                break
            picked.append(line)
            used += len(line) + 1
        picked.reverse()

        # 成对对齐：丢掉因裁剪产生的"孤立回答"开场（若只剩回答则保留，避免整个活记忆变空）
        if any(p.startswith("用户:") for p in picked):
            head = 0
            while head < len(picked) and picked[head].startswith("助手:"):
                head += 1
            picked = picked[head:]

        parts = [p for p in (extra, memory, "\n".join(picked)) if p]
        out = "\n".join(parts)
        if len(out) > limit:
            # 兜底硬上限：分隔符与省略标记也要计入预算，否则边界上会超出几字符
            out = out[:limit]
        logger.debug(
            "[lead_agent] digest_assembled chars=%s tokens≈%s turns=%s",
            len(out), estimate_tokens(out), len(picked),
        )
        return out


class LeadAgent:
    """主导 Agent：意图 → 决策 → 调度 → 汇报 → 记忆回流。"""

    def __init__(self, llm, *, observer=None, extra_plannable_tools=None, config=None) -> None:
        self.llm = llm
        self.observer = observer
        self.extra_plannable_tools = set(extra_plannable_tools or ())
        self.config = config
        self._memo: dict = {}
        self._memo_lock = asyncio.Lock()

    # ── 对外主入口：异步事件流 ──
    async def stream(
        self,
        user_msg: str,
        *,
        ctx: LeadContext,
        db_session,
    ) -> AsyncIterator[dict]:
        """处理一轮用户输入，产出 SSE 事件流。"""
        degradation: list[str] = []
        # 请求级画布状态感知：一次构造、全请求复用（内部缓存，落块后按需 force 重读）。
        # 所有消费点（决策 / 回答 / Worker / 收尾摘要）统一从这里取，避免"逐点注入漏一处"。
        if ctx.canvas_state is None:
            from app.services.agents.lead.canvas_state import CanvasStateProvider

            ctx.canvas_state = CanvasStateProvider(
                db_session, ctx.user_id, ctx.canvas_id, entry=ctx.entry
            )
        observer = self.observer
        if observer is None:
            try:
                from app.services.observability import get_observer
                observer = get_observer()
            except Exception:  # noqa: BLE001
                observer = None

        trace = None
        trace_cm = None  # contextmanager 对象，收尾时 __exit__ 关闭
        if observer is not None:
            try:
                trace_cm = observer.trace(
                    "lead_agent_turn",
                    user_id=str(ctx.user_id),
                    session_id=ctx.session_id or None,
                    metadata={"entry": ctx.entry, "user_msg_length": len(user_msg or "")},
                )
                trace = trace_cm.__enter__()
            except Exception:  # noqa: BLE001
                trace = None
                trace_cm = None

        try:
            # ── 1) 首轮合并调用：意图 + 决策 一次 LLM 请求（省一次调用，语义同源）──
            available_datasources = await _load_available_datasources(
                db_session, str(ctx.user_id), ctx.datasource_id,
                inject_fields_unselected=(ctx.entry == "canvas"),
            )
            # 画布感知（对 Supervisor）：读库中已落盘的画布布局，注入决策 prompt，
            # 让 Lead 知道"画布上现已落成什么图表/文本、布局如何"（完成的真实效果）。
            # 走请求级 provider（带缓存）：同一轮多处消费不会重复读库。
            canvas_layout = await ctx.canvas_state.layout() if ctx.canvas_state else ""
            merged = await decide_action_merged(
                user_msg,
                history_summary=ctx.digest(),
                datasources=available_datasources,
                subtask_summaries=ctx.turn_summaries,
                canvas_layout=canvas_layout,
                llm=self.llm,
                timeout=settings.LEAD_DECISION_TIMEOUT,
                degradation=degradation,
                trace=trace,
            )
            intent = merged.intent
            ctx.add_turn("user", user_msg)
            yield {
                "type": "intent",
                "intent": intent.intent.value,
                "confidence": intent.confidence,
                "needs_plan": intent.needs_plan,
                "degraded": intent.degraded,
            }

            # ── 2) Supervisor 循环：第 0 轮复用合并决策，后续轮次独立决策，直到 stop / 轮次上限 ──
            max_rounds = getattr(settings, "LEAD_MAX_SUPERVISOR_ROUNDS", 4)
            stopped = False        # 是否经决策器 STOP 收敛
            had_analysis = False   # 本轮回是否已派发过分析（用于轮次耗尽提示）
            prev_action: str | None = None  # 上一轮动作（决策输入，prompt 据此硬化 stop）
            for rnd in range(max_rounds):
                if rnd == 0:
                    decision = merged.decision
                else:
                    # 每轮决策前刷新画布快照：上一轮子任务落块后（前端已写库），DB 里的画布
                    # 已变化 → force=True 跳过缓存重读
                    if ctx.canvas_state is not None:
                        canvas_layout = await ctx.canvas_state.layout(force=True) or canvas_layout
                    decision = await decide_action(
                        user_msg,
                        intent,
                        history_summary=ctx.digest(),
                        datasources=available_datasources,
                        subtask_summaries=ctx.turn_summaries,
                        canvas_layout=canvas_layout,
                        prev_action=prev_action,
                        llm=self.llm,
                        timeout=settings.LEAD_DECISION_TIMEOUT,
                        degradation=degradation,
                        trace=trace,
                    )
                prev_action = decision.action.value
                yield {
                    "type": "decision",
                    "round": rnd,
                    "action": decision.action.value,
                    "tool": decision.tool_name,
                    "reason": decision.reason,
                    "degraded": decision.degraded,
                }

                # 反问：输出反问后本轮回结束，等用户下一轮消息带回目标。
                # 不能直接 return：必须走下方统一收尾（_maybe_final_summary / 记忆回流 / done），
                # 否则前端收不到 done 事件，流式状态永远停在"执行中"。
                if decision.action == ActionType.ASK_USER:
                    async for ev in self._emit_answer(decision.direct_text or "请补充更多信息，我好帮你继续。"):
                        yield ev
                    break
                # 主管收尾
                if decision.action == ActionType.STOP:
                    stopped = True
                    break
                # 降级防御：首轮降级按原兜底行为执行一次；非首轮降级意味着 LLM 已不可用，
                # 直接收尾（避免兜底路径在每轮重复触发分析，白白消耗预算）
                if decision.degraded and rnd > 0:
                    break
                if decision.action == ActionType.CALL_ANALYSIS:
                    had_analysis = True
                    async for ev in self._run_analysis_branch(
                        user_msg, decision, ctx, db_session, available_datasources,
                        round_idx=rnd, degradation=degradation, trace=trace,
                    ):
                        yield ev
                    # 子任务执行完毕 → 继续下一轮决策（靠 subtask_summaries 注入 + prompt 硬规则
                    # 决定 stop，避免重复执行同目标；run_analysis memo 幂等兜底）
                else:
                    async for ev in self._answer_branch(
                        user_msg, decision, ctx, canvas_layout=canvas_layout, trace=trace
                    ):
                        yield ev
                    # 纯回答路径：答完即代码层收尾，不再开下一轮决策。
                    # 曾经靠 prompt 硬规则（"上一轮为 answer 必须 stop"）让 LLM 自觉收敛，
                    # 实测 LLM 连续多轮重复输出 answer（同类问题每轮 reason 不同、回答重复），
                    # 收敛闸必须落在代码层，不依赖 LLM 自觉。
                    break
            else:
                # 循环走满（无 break）仍未 stop：轮次上限耗尽 → 透明提示 + 记录可观测
                if had_analysis:
                    degradation.append("lead_max_rounds_reached")
                    yield {
                        "type": "status",
                        "message": (
                            f"本轮已连续派发 {max_rounds} 个子任务仍未收尾，已停止；"
                            "如需继续请再说。"
                        ),
                    }

            # ── 2.5) 收尾总结：多子任务完成时给一句汇总（纯文本，不额外调 LLM）──
            async for ev in self._maybe_final_summary(ctx):
                yield ev

            # ── 3) 记忆回流（水位驱动的累积合并：旧长期记忆 + 未合并对话段 → 新累积摘要）──
            # 事件一律交给 API 层的 _save_memory 决策落库（含失败计数、水位推进）：
            # 只有真正产出摘要时才向用户下发 memory_saved；失败/仅推进水位的事件
            # 由 _save_memory 消费但不外发。
            memory_event = await self._maybe_summarize(ctx, db_session=db_session, trace=trace)
            if memory_event:
                if memory_event.get("summary"):
                    yield {"type": "memory_saved", "chars": len(memory_event["summary"])}
                yield {"type": "compressed_history", **memory_event}

            yield {"type": "done", "degraded": bool(degradation), "degradations": degradation}
        finally:
            if trace_cm is not None:
                try:
                    trace_cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    # ── 执行器选择（确定性，不经额外 LLM 判断，省一次调用）──
    async def _resolve_subtask_mode(
        self,
        entry: str,
        decision: Decision,
        constraints: dict,
    ) -> str:
        """确定 run_analysis 的执行内核 mode。

        分工：
        - 显式策略参数优先（调用方可强制 react / orchestrator / canvas）；
        - 画布入口（entry=canvas）→ mode=canvas（CanvasOrchestrator），
          因为对话页的工具白名单不含画布工具、画布页的记忆与上下文都绑定 canvas；
        - 对话入口 → 由 Supervisor 决策顺带输出的 `decision.complexity` 决定：
          complex → orchestrator（AgentOrchestrator），simple → react（ReactGraphAgent）。
          复杂度不再单独调用旧路由分类器，避免每轮多一次 LLM 调用。
        """
        explicit = (constraints or {}).get("mode")
        if explicit:
            return str(explicit)
        if entry == "canvas":
            # 画布页也复用 Supervisor 顺带输出的 complexity（零新增 LLM 调用）：
            # simple → react（轻量单目标，可带画布工具），complex → canvas（CanvasOrchestrator）
            return "canvas" if getattr(decision, "complexity", "complex") == "complex" else "react"
        return "orchestrator" if getattr(decision, "complexity", "complex") == "complex" else "react"

    @staticmethod
    def _build_execution_summary(
        result: RunAnalysisResult,
        canvas_layout: str = "",
    ) -> str:
        """把一次 run_analysis 的执行产物组装成结构化摘要，供 Supervisor 决策参考。

        三块信息（业界 subagents 模式的 worker 回传口径，但不带完整 SQL/全量结果）：
          1. 子任务完成了什么（成败 + 产物数 + 规划）
          2. 执行链路（tool_chain：工具名 + 参数摘要 + 行数 + 成败）
          3. 证据与报告（数值校验结果 + 报告全文，避免 240 字截断导致 Lead 无法判断"达没达成"）
        canvas_layout：子任务落块后数据库里已落盘的画布布局快照（画布入口传入），
        让 Supervisor 连"画布上现在到底有什么、是什么图表"都一目了然。
        """
        lines: list[str] = ["[子任务执行记录]"]
        # 1. 概况
        if result.success:
            if result.blocks_added > 0:
                lines.append(f"  动作：分析完成，在画布上新增 {result.blocks_added} 个内容块。")
            else:
                lines.append("  动作：分析完成，结果已生成。")
        else:
            lines.append(f"  动作：分析未完成。原因：{result.error or '未知'}。")
        # 2. 规划步骤（若有）
        if result.steps:
            plan_text = " → ".join(
                str(s.get("title") or s.get("content") or s.get("objective") or "步骤")[:40]
                for s in result.steps[:6]
            )
            lines.append(f"  规划[{len(result.steps)}步]：{plan_text}")
        # 3. 执行链路（工具 + 参数摘要 + 行数 + 成败）
        if result.tool_chain:
            chain_parts: list[str] = []
            for tc in result.tool_chain[:12]:
                ok_mark = "✓" if tc.get("ok") else "✗"
                rows = f",{tc['rows']}行" if tc.get("rows") is not None else ""
                args = f"({tc.get('args', '')})" if tc.get("args") else ""
                chain_parts.append(f"{tc.get('name','?')}{args}{rows}{ok_mark}")
            lines.append(f"  工具链：{' → '.join(chain_parts)}")
        # 4. 数值证据 + 校验结果
        if result.tool_chain:
            verified_mark = "已验证" if result.verified else "⚠️部分数字未经查询结果证实"
            lines.append(f"  数值：{len(result.tool_chain)} 次工具调用，报告数字 {verified_mark}。")
        # 5. 画布现状（已落盘的真实效果：有多少块、是什么图表/文本、布局）
        if canvas_layout:
            one_line = canvas_layout.replace("\n", "；")
            lines.append(f"  画布现状：{one_line[:400]}")
        if result.error:
            lines.append(f"  错误：{result.error[:200]}")
        summary = "\n".join(lines)
        # 6. 报告全文（truncate 1500，不再砍到 240）
        if result.report:
            report_excerpt = result.report[:1500]
            summary += f"\n[子任务产出报告]\n{report_excerpt}"
        return summary

    # ── 分支：调用 run_analysis（透传 + 旁路感知）──
    async def _run_analysis_branch(
        self,
        user_msg: str,
        decision: Decision,
        ctx: LeadContext,
        db_session,
        available_datasources: list[dict],
        round_idx: int = 0,
        degradation: list[str] | None = None,
        trace=None,
    ) -> AsyncIterator[dict]:
        tool_args = decision.tool_args or {}
        goal = str(tool_args.get("goal") or user_msg)
        constraints = dict(tool_args.get("constraints") or {})
        # 执行器选择（确定性函数，不经额外 LLM 判断）：
        # - 显式策略参数优先（调用方可强制 react/orchestrator/canvas）
        # - 画布入口 → canvas 编排（CanvasOrchestrator）
        # - 对话入口 → 用 Supervisor 决策顺带输出的 complexity（省一次分类调用）
        mode = await self._resolve_subtask_mode(ctx.entry, decision, constraints)
        args = RunAnalysisArgs(
            goal=goal,
            datasource_id=ctx.datasource_id,
            canvas_id=ctx.canvas_id,
            entry=ctx.entry,
            constraints={"mode": mode, **constraints},
        )
        yield {"type": "tool_call", "name": "run_analysis", "round": round_idx, "args": {
            "goal": goal, "entry": ctx.entry, "mode": mode,
        }}

        out_q: asyncio.Queue = asyncio.Queue()
        holder: dict = {}

        async def _forward(ev: dict) -> None:
            # 子执行器（CanvasOrchestrator / AgentOrchestrator / legacy）结束时也会 emit
            # {"type":"done"}，那是"子任务结束"的内部标记，不能透传给客户端：
            # 否则前端会在子任务落块完就把流标记完成（解锁输入、清 streaming 状态），
            # 而外层 Lead 还在继续收尾（决策 stop / 记忆回流），出现"一次请求两个 done"。
            if isinstance(ev, dict) and ev.get("type") == "done":
                return
            await out_q.put(ev)

        async def _on_progress(p: StepProgress) -> None:
            # progress 事件只供 ActivityFeed 展示，不 emit text 到主气泡
            # （主气泡只保留最终总结，工具执行细节由工作台卡片承载）
            await out_q.put({
                "type": "progress",
                "round": round_idx,
                "index": p.index,
                "total": p.total,
                "title": p.title,
                "status": p.status,
                "note": p.note,
                "tool": p.tool,
            })
            # 仅失败时额外 emit 一条 text 到主气泡（错误提示）
            if p.status == "fail":
                text = render_progress_text(p)
                ctx.turn_summaries.append(text)
                await out_q.put({"type": "text", "content": text})

        async def _runner() -> None:
            try:
                async with self._memo_lock:
                    holder["result"] = await run_analysis(
                        args,
                        db_session=db_session,
                        emit=_forward,
                        llm=self.llm,
                        trace=trace,
                        lead_ctx=ctx,
                        extra_plannable_tools=self.extra_plannable_tools,
                        on_progress=_on_progress,
                        memo=self._memo,
                        available_datasources=available_datasources,
                        worker_guidance=getattr(decision, "guidance", "") or "",
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[lead_agent] run_analysis_failed: {e}")
                holder["error"] = e
            finally:
                await out_q.put(None)

        runner = asyncio.create_task(_runner())
        while True:
            ev = await out_q.get()
            if ev is None:
                break
            yield ev
        await runner

        result: RunAnalysisResult | None = holder.get("result")
        if result is None:
            err = holder.get("error")
            yield {
                "type": "tool_result",
                "name": "run_analysis",
                "result": json_dumps({"error": f"分析执行失败: {err}"}),
                "ok": False,
            }
            yield {"type": "text", "content": "分析执行失败，请稍后重试或换一种问法。"}
            return

        yield {
            "type": "tool_result",
            "name": "run_analysis",
            "result": json_dumps({
                "ok": result.success,
                "steps": len(result.steps),
                "blocks_added": result.blocks_added,
                "elapsed_ms": result.elapsed_ms,
                "error": result.error,
            }),
            "ok": result.success,
        }
        # 主导 Agent 对话感：分析完成后给出一句自然语言总结（不是工具记录，是"人话"）
        # 同时把结构化执行摘要回写 turn_summaries，供 Supervisor 下一轮决策参考
        # （含工具链/规划/数值证据/报告全文，避免 240 字截断导致 Lead 不知道"做了什么、效果如何"）
        # 先做确定性评估（成功/失败都要评），写入 turn_summaries 供决策器单独注入。
        assessment = assess_subtask(result, entry=ctx.entry, user_goal=goal)
        ctx.turn_summaries.append(assessment)
        # 画布叙事要点（结论在文本块里，模板句不含要点）：提前取出，
        # 既用于收尾话术，也让记忆存储的是"FreshFoods 8,640万居首"这类要点而非空模板句。
        canvas_highlight = ""
        if result.success and ctx.canvas_state is not None:
            try:
                canvas_highlight = await ctx.canvas_state.narrative() or ""
            except Exception:  # noqa: BLE001
                canvas_highlight = ""
        if result.success:
            blocks = result.blocks_added
            # 落块后前端已实时保存到 DB → force=True 强制重读，让 Supervisor 看到"落的到底是什么"
            # （provider 有请求级缓存，这里必须绕过缓存，否则拿到的是本轮开始前的旧布局）
            canvas_now = (
                await ctx.canvas_state.layout(force=True) if ctx.canvas_state is not None else ""
            )
            ctx.turn_summaries.append(self._build_execution_summary(result, canvas_layout=canvas_now))
            if blocks > 0:
                if canvas_highlight:
                    summary = f"分析完成，已生成 {blocks} 个内容块。要点：{canvas_highlight}（详见画布）"
                else:
                    summary = f"分析完成，已在画布上添加了 {blocks} 个内容块，你可以直接查看和调整。"
            else:
                summary = "分析完成，结果已生成，请查看上方内容。"
            yield {"type": "text", "content": summary}
        if result.report:
            yield {"type": "report", "content": result.report, "source": result.report_source}
            # 记忆落点：画布路径存要点（真实结论），对话路径存 report（已是浓缩分析文本）。
            # 两者都避免把"分析完成，添加了 N 个块"这类空模板句写进会话记忆。
            memory_text = result.report
            if canvas_highlight:
                memory_text = (f"分析完成，已生成 {result.blocks_added} 个内容块（画布）。"
                               f"要点：{canvas_highlight}")
            ctx.add_turn("assistant", memory_text)
        elif result.error:
            yield {"type": "status", "message": f"本次分析未产出报告：{result.error}"}

    # ── 分支：直接回答（decision 已给文本则直出，否则轻量 LLM 生成）──
    async def _answer_branch(
        self,
        user_msg: str,
        decision: Decision,
        ctx: LeadContext,
        canvas_layout: str = "",
        trace=None,
    ) -> AsyncIterator[dict]:
        text = (decision.direct_text or "").strip()
        if text:
            async for ev in self._emit_answer(text):
                yield ev
            return
        messages = [{"role": "system", "content": _ANSWER_SYSTEM}]
        if ctx.history_summary.strip():
            # 长期记忆也要给上限：legacy 摘要可能上千字，原样注入会挤占追问所需的空间
            mem = ctx.history_summary.strip()
            mem_cap = max(200, int(getattr(settings, "LEAD_CTX_MAX_CHARS", 4000) or 4000) // 3)
            if len(mem) > mem_cap:
                mem = mem[:mem_cap] + "…"
            messages.append({"role": "assistant", "content": f"【历史记忆】{mem}"})
        if ctx.metrics_ctx.strip():
            messages.append({"role": "assistant", "content": f"【受治理指标清单】\n{ctx.metrics_ctx}"})
        if canvas_layout.strip():
            # 画布感知也要覆盖"直接回答"分支：此前只有决策与 Worker 有画布快照，
            # 于是"画布上有什么？""我刚让你画的是什么？"这类短问句若被路由成 answer
            # 就会失去画布信息（能力层已有 provider，这里取用即可，不额外读库）。
            messages.append({
                "role": "assistant",
                "content": f"【当前画布布局（已落盘，回答涉及画布时以此为准）】\n{canvas_layout}",
            })
        for t in ctx.recent_turns(max_turns=8):
            messages.append({"role": t["role"], "content": t["content"]})
        messages.append({"role": "user", "content": user_msg})
        collected: list[str] = []
        try:
            if trace is not None:
                from app.services.observability import observe_llm_call

                with observe_llm_call(trace, "lead_answer", messages=messages,
                                      model=settings.openai_model):
                    async for delta in self.llm.stream_chat(messages, temperature=0.5, max_tokens=1200):
                        if delta:
                            collected.append(delta)
                            yield {"type": "text", "content": delta}
            else:
                async for delta in self.llm.stream_chat(messages, temperature=0.5, max_tokens=1200):
                    if delta:
                        collected.append(delta)
                        yield {"type": "text", "content": delta}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lead_agent] answer_llm_failed: {e}")
            yield {"type": "text", "content": "抱歉，我暂时无法回答这个问题，请稍后重试。"}
            return
        answer = "".join(collected).strip()
        if answer:
            yield {"type": "report", "content": answer, "source": "lead"}
            ctx.add_turn("assistant", answer)

    async def _emit_answer(self, text: str) -> AsyncIterator[dict]:
        """把一段确定性文本按行切片吐出（保持流式观感）。"""
        for line in (text or "").splitlines(keepends=True) or [text]:
            if line:
                yield {"type": "text", "content": line}
        if text and not text.endswith("\n"):
            pass
        yield {"type": "report", "content": text, "source": "lead"}

    # ── 收尾总结：多子任务完成后给一句汇总（纯文本，不调 LLM）──
    async def _maybe_final_summary(self, ctx: LeadContext) -> AsyncIterator[dict]:
        """>=2 次子任务完成时，拼一句"共完成…"收尾（纯确定性，零 token 成本）。

        单任务/纯问答场景已由正文与"分析完成"话术覆盖，不重复总结。
        """
        # 兼容新旧两种摘要头：旧版 "[子任务完成]"，新版 "[子任务执行记录]"
        done_marks = [s for s in ctx.turn_summaries if "[子任务完成]" in s or "[子任务执行记录]" in s]
        if len(done_marks) < 2:
            return
        analysis_count = len(done_marks)
        block_count = sum(1 for s in done_marks if "添加了" in s or "新增" in s)
        summary: list[str] = [f"完成 {analysis_count} 项分析"]
        if block_count > 0:
            summary.append(f"画布新增 {block_count} 个内容块")
        text = "，".join(summary) + "。"
        yield {"type": "text", "content": text}
        ctx.turn_summaries.append(f"[总结] {text}")

    # ── 记忆回流：累积合并（旧长期记忆 + 未合并对话段 → 新累积摘要），API 层持久化 ──
    async def _maybe_summarize(self, ctx: LeadContext, db_session=None, trace=None) -> dict | None:
        """跨轮记忆累积合并（水位驱动；借鉴 session memory compact 的 lastSummarizedMessageId）。

        语义：
        - **触发**：未并入的用户轮数 ≥ LEAD_MEMORY_MERGE_ROUNDS，且未触发熔断；
        - **内容**：水位之后**最早**的一段消息（不是"最近一段"）→ 逐轮把水位向后推。
          积压超过窗口时不会留下永久空洞（旧实现会：窗口外的中间段被永久跳过，
          却被进度标记成"已并入"）；
        - **摘要**：输入 =【旧长期记忆】+【新增对话】→ 覆盖写新累积值（旧记忆参与，不丢）；
        - **失败**：不写摘要、不推进水位，只累加连续失败计数；达 LEAD_MEMORY_MAX_FAILURES
          后熔断不再重试，下次成功合并时清零。
        返回 None = 本轮未触发或已熔断，不产生任何落库事件。
        """
        min_rounds = max(1, int(getattr(settings, "LEAD_MEMORY_MERGE_ROUNDS", 4) or 4))
        max_failures = max(1, int(getattr(settings, "LEAD_MEMORY_MAX_FAILURES", 3) or 3))
        max_messages = max(2, int(getattr(settings, "LEAD_MEMORY_MAX_MERGE_MESSAGES", 24) or 24))

        if int(ctx.memory_fail_count or 0) >= max_failures:
            logger.warning(
                "[lead_agent] memory_merge_circuit_open: 连续失败 %s 次，本轮跳过",
                ctx.memory_fail_count,
            )
            return None
        total_rounds = await _count_session_user_rounds(db_session, ctx.session_id)
        if not _should_merge_memory(total_rounds, ctx.memory_covered, min_rounds):
            return None
        progress = _resolve_memory_progress(total_rounds, ctx.memory_covered)
        new_messages, last_id = await _load_unmerged_messages(
            db_session, ctx.session_id, after_id=ctx.memory_watermark, max_messages=max_messages
        )
        if last_id is None:
            return None
        # 诚实计数：只记"实际并入了多少用户轮"，不再写推算出来的 U-1
        merged_rounds = sum(1 for m in new_messages if m.get("role") == "user")
        next_covered = min(progress + merged_rounds, max(0, total_rounds - 1))
        if not new_messages:
            # 本段全是空占位：不花 LLM，只把水位推过去（否则会永远卡在这段上）
            logger.info("[lead_agent] memory_segment_empty: 仅推进水位")
            ctx.memory_covered = next_covered
            ctx.memory_watermark = last_id
            return {
                "source": "lead",
                "summary": None,
                "covered_rounds": next_covered,
                "last_merged_message_id": last_id,
            }
        text = _build_memory_merge_input(ctx.history_summary, new_messages)
        if not text.strip():
            return None
        system_prompt = _memory_merge_system_prompt()
        max_out = max(300, int(getattr(settings, "LEAD_MEMORY_SUMMARY_MAX_TOKENS", 900) or 900))
        try:
            if trace is not None:
                from app.services.observability import observe_llm_call

                with observe_llm_call(trace, "lead_memory_summary",
                                      messages=[{"role": "system", "content": system_prompt},
                                                {"role": "user", "content": text}]) as span:
                    result = await self.llm.complete(
                        [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": text},
                        ],
                        temperature=0.2,
                        max_tokens=max_out,
                        enable_thinking=False,
                        return_usage=True,
                    )
                    summary, usage_meta = result if isinstance(result, tuple) else (result, None)
                    if usage_meta:
                        span.update(usage=usage_meta)
            else:
                summary = await self.llm.complete(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.2,
                    max_tokens=max_out,
                    enable_thinking=False,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lead_agent] memory_merge_failed: {e}")
            return {"source": "lead", "failed": True}
        summary = _extract_summary(summary or "")
        if not summary:
            logger.warning("[lead_agent] memory_merge_empty_summary")
            return {"source": "lead", "failed": True}
        ctx.memory_covered = next_covered
        ctx.memory_watermark = last_id
        ctx.turn_summaries.append(summary)
        return {
            "source": "lead",
            "summary": summary,
            "covered_rounds": next_covered,
            "last_merged_message_id": last_id,
        }


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
