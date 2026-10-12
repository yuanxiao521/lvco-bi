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
    MergedOutcome,
    decide_action,
    decide_action_merged,
)
from app.services.agents.lead.lead_dispatch import run_worker
from app.services.agents.lead.lead_intent import IntentResult, IntentType
from app.services.agents.lead.lead_worker import resolve_worker
from app.services.lead_session_state import lead_session_state, read_phase
from app.services.agents.lead.lead_tools import _load_available_datasources

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
    max_chars = max(200, int(getattr(settings, "LEAD_MEMORY_SUMMARY_TOKENS", 600) or 600))
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


def _is_affirmative(msg: str) -> bool:
    """判定是否为"对上一轮反问的纯确认"回复（确认 / 是的 / 好 / ok…）——文本兜底路径。

    主路径是前端确认卡片（ui_action 显式信号，零猜测）；本函数只在用户偏要
    打字回复时兜底。只认短回复：长回复通常夹带新指令（"是的，不过改成折线图"），
    那种情况应走正常决策。排除寒暄词（"你好"含"好"，会误触发恢复）。
    """
    s = (msg or "").strip().lower().rstrip("。！!.,，、~ ")
    if not s or len(s) > 8:
        return False
    if s in ("你好", "您好", "hello", "hi", "嗨", "在吗"):
        return False
    return any(a in s for a in ("确认", "确定", "是的", "对的", "好", "可以", "嗯", "ok", "yes"))


def _guard_stop_outcome(guard_text: str) -> MergedOutcome:
    """确认取消 / 过期的确定性收尾：合成 STOP，主管循环第一轮即收敛（answer 文本由 STOP 分支吐出）。"""
    return MergedOutcome(
        intent=IntentResult(
            intent=IntentType.CHAT, confidence=1.0, needs_plan=False, degraded=False,
        ),
        decision=Decision(
            action=ActionType.STOP, direct_text=guard_text, reason="confirm_guard",
        ),
    )


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
    # HITL 确认卡片动作：{"type": "confirm" | "cancel"}（前端按钮点击时由入口透传）。
    # 守卫据此在决策之前短路——显式信号驱动状态迁移，不靠对自然语言的猜测。
    ui_action: dict | None = None

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

        P0 起实现迁至 context.sources.window.select_for_answer（唯一实现）；
        本方法保留签名作为兼容入口，行为逐字节等价（由 test_context_assembly 锁住）。
        """
        from app.services.context.sources.window import select_for_answer

        return select_for_answer(self.turns, max_turns=max_turns, max_chars=max_chars)

    def digest(self, max_chars: int | None = None) -> str:
        """给子任务/决策注入的紧凑上下文（额外上下文 + 长期摘要 + 最近轮次）。

        装配规则（由"尾部硬切"改为"整条消息装配"）：
        - **不切句子**：活记忆按"整条消息"从新到旧累加，装不下就停；只有单条自身就超预算时
          才截断该条（旧实现是从整体尾部一刀切，会把一条消息切掉半个句子）；
        - **长期记忆优先但有上限**：先给【长期记忆】，但为其设上限 `预算 − 活记忆预留`，
          避免摘要变长时把"刚才这几轮"整个挤掉；反过来也不会让旧记忆把活记忆饿死；
        - **成对对齐**：裁剪后若开头是孤立 assistant（没有提问的回答），丢掉它——
          这种开场既占预算又容易误导（见 context_utils.align_history_pairs）。

        P0 起实现迁至 context.assembler.render_window_for_decision（唯一实现）；
        本方法保留签名作为兼容入口，行为逐字节等价（由 test_context_assembly 锁住）。
        """
        from app.services.context.assembler import render_window_for_decision

        out = render_window_for_decision(
            self.turns,
            extra_context=self.extra_context,
            history_summary=self.history_summary,
            max_chars=max_chars,
        )
        logger.debug(
            "[lead_agent] digest_assembled chars=%s tokens≈%s",
            len(out), estimate_tokens(out),
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
            # ── 0) 确认守卫（HITL gate）：前端确认卡片的显式信号优先，文本"确认"兜底。
            # 命中即短路 merged LLM 调用（绕开超时降级风险）；恢复走与正常派发
            # 完全相同的执行/评审/收尾路径——只是跳过了"重新理解意图"这一步。
            # 状态观测：守卫 pop 槽之前读（pop 后 AWAITING_CONFIRM 就变 IDLE 了）
            try:
                logger.info("[lead] phase=%s entry=%s", read_phase(ctx.session_id).value, ctx.entry)
            except Exception:  # noqa: BLE001
                pass
            _guard = await self._confirm_guard(ctx, user_msg)
            if _guard is None:
                merged = await decide_action_merged(
                    user_msg,
                    entry=ctx.entry,
                    history_summary=ctx.digest(),
                    datasources=available_datasources,
                    subtask_summaries=ctx.turn_summaries,
                    canvas_layout=canvas_layout,
                    llm=self.llm,
                    timeout=settings.LEAD_DECISION_TIMEOUT,
                    degradation=degradation,
                    trace=trace,
                )
            elif _guard[0] == "resume":
                merged = MergedOutcome(
                    intent=IntentResult(
                        intent=IntentType.FOLLOWUP, confidence=1.0,
                        needs_plan=False, degraded=False,
                    ),
                    decision=_guard[1],
                )
            else:
                # cancel / 确认过期：确定性文本收尾（合成 STOP，主管循环第一轮即收敛）
                merged = _guard_stop_outcome(_guard[1])
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
                    # 挂起本轮待确认目标（Redis 槽，跨请求存活，TTL 与会话锁对齐）；
                    # 封闭确认（ask_kind=confirm）额外发 confirm_request 事件，
                    # 前端渲染确认卡片——用户点按钮产生显式 ui_action，下一请求守卫直接恢复。
                    lead_session_state.set_pending(ctx.session_id, str(user_msg or ""))
                    async for ev in self._emit_answer(decision.direct_text or "请补充更多信息，我好帮你继续。"):
                        yield ev
                    if getattr(decision, "ask_kind", "clarify") == "confirm":
                        yield {
                            "type": "confirm_request",
                            "goal": str(user_msg or ""),
                            "question": decision.direct_text or "",
                        }
                    break
                # 主管收尾
                if decision.action == ActionType.STOP:
                    stopped = True
                    # 守卫的取消/过期收尾经由此处输出确定性话术（正常 STOP 的 direct_text 为空，不受影响）
                    if decision.direct_text:
                        async for ev in self._emit_answer(decision.direct_text):
                            yield ev
                    break
                # 降级防御：首轮降级按原兜底行为执行一次；非首轮降级意味着 LLM 已不可用，
                # 直接收尾（避免兜底路径在每轮重复触发分析，白白消耗预算）
                if decision.degraded and rnd > 0:
                    break
                if decision.action == ActionType.CALL_ANALYSIS:
                    had_analysis = True
                    # 派发目标由「入口 × 复杂度」表解析（lead_worker）；显式 constraints.mode 优先
                    worker = resolve_worker(
                        ctx.entry,
                        getattr(decision, "complexity", "complex"),
                        ((decision.tool_args or {}).get("constraints") or {}).get("mode"),
                    )
                    async for ev in run_worker(
                        user_msg=user_msg, decision=decision, ctx=ctx, worker=worker,
                        db_session=db_session, available_datasources=available_datasources,
                        llm=self.llm, memo=self._memo, memo_lock=self._memo_lock,
                        extra_plannable_tools=self.extra_plannable_tools,
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
        for t in ctx.recent_turns(max_turns=12):  # AB-B: 8->12
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

    async def _confirm_guard(self, ctx: LeadContext, user_msg: str) -> tuple[str, object] | None:
        """确认守卫（HITL gate，决策之前拦截）。

        返回：
            None                   → 无挂起 / 非确认场景，走正常 merged 决策
            ("resume", Decision)   → 恢复派发上轮挂起目标（跳过 LLM，直接进执行分支）
            ("text", str)          → 取消 / 确认过期，确定性文本收尾
        """
        ui = ctx.ui_action if isinstance(ctx.ui_action, dict) else {}
        action_type = str(ui.get("type") or "").strip().lower()
        pending = lead_session_state.pop_pending(ctx.session_id)

        if action_type == "confirm":
            if pending:
                logger.info("[lead] confirm_gate_resume goal=%s", pending[:40])
                return ("resume", Decision(
                    action=ActionType.CALL_ANALYSIS,
                    tool_name="run_analysis",
                    tool_args={"goal": pending},
                    reason="用户通过确认卡片恢复执行上轮目标",
                    degraded=False,
                    complexity="simple",
                ))
            return ("text", "这个确认已经过期了，请重新告诉我你想做什么。")

        if action_type == "cancel":
            logger.info("[lead] confirm_gate_cancel goal=%s", (pending or "")[:40])
            return ("text", "好的，已取消，画布保持原样。有新需求随时告诉我。")

        # 无显式信号：用户偏要打字回复的文本兜底（pop 已取走；非肯定 → 目标丢弃，走正常决策）
        if pending and _is_affirmative(user_msg):
            logger.info("[lead] confirm_text_resume goal=%s", pending[:40])
            return ("resume", Decision(
                action=ActionType.CALL_ANALYSIS,
                tool_name="run_analysis",
                tool_args={"goal": pending},
                reason="用户文本确认，恢复执行上轮目标",
                degraded=False,
                complexity="simple",
            ))
        return None

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
