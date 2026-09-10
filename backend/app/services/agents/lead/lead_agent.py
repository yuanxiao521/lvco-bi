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
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.config import settings
from app.services.agents.lead.lead_decider import ActionType, Decision, decide_action
from app.services.agents.lead.lead_intent import IntentResult, IntentType, classify_intent
from app.services.agents.lead.lead_perception import StepProgress, render_progress_text
from app.services.agents.lead.lead_tools import (
    RunAnalysisArgs,
    RunAnalysisResult,
    _load_available_datasources,
    run_analysis,
)

logger = logging.getLogger("lvco.lead.agent")

_ANSWER_SYSTEM = (
    "你是 LvcoBI 的数据智能助手。用简洁、专业的中文回答用户问题。"
    "如果问题与数据/分析相关但缺少必要信息（数据源、指标、时间范围），"
    "请礼貌地说明需要哪些信息，并给出下一步建议。不要编造数据。"
)

_SUMMARY_SYSTEM = (
    "把下面这段对话压缩为不超过 200 字的中文摘要，"
    "保留关键事实、涉及的数据源、已完成的动作与仍未解决的问题。只输出摘要正文。"
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
    turns: list[dict] = field(default_factory=list)          # 会话活记忆
    turn_summaries: list[str] = field(default_factory=list)  # 关键节点汇报累积
    _summarized_upto: int = 0                 # 已纳入摘要的 turns 边界（内部）

    def add_turn(self, role: str, content: str) -> None:
        """追加一轮对话（自动裁剪窗口，避免上下文膨胀）。"""
        if content is None:
            return
        self.turns.append({"role": role, "content": str(content)})
        cap = max(1, settings.LEAD_MAX_TURNS_IN_CTX) * 2
        if len(self.turns) > cap:
            dropped = len(self.turns) - cap
            self.turns = self.turns[dropped:]
            self._summarized_upto = max(0, self._summarized_upto - dropped)

    def digest(self, max_chars: int = 4000) -> str:
        """给决策注入的紧凑上下文（长期摘要 + 最近轮次）。"""
        parts: list[str] = []
        if self.history_summary.strip():
            parts.append(f"【长期记忆】{self.history_summary.strip()}")
        for t in self.turns[-max(1, settings.LEAD_MAX_TURNS_IN_CTX):]:
            role = "用户" if t.get("role") == "user" else "助手"
            parts.append(f"{role}: {str(t.get('content', ''))}")
        text = "\n".join(parts)
        return text if len(text) <= max_chars else text[-max_chars:]


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
        observer = self.observer
        if observer is None:
            try:
                from app.services.observability import get_observer
                observer = get_observer()
            except Exception:  # noqa: BLE001
                observer = None

        trace = None
        if observer is not None:
            try:
                trace = observer.trace(
                    "lead_agent_turn",
                    user_id=str(ctx.user_id),
                    session_id=ctx.session_id or None,
                    metadata={"entry": ctx.entry, "user_msg_length": len(user_msg or "")},
                )
                trace.__enter__()
            except Exception:  # noqa: BLE001
                trace = None

        try:
            # ── 1) 意图识别（Supervisor 第 1 轮专用，主目标全程固定）──
            intent = await classify_intent(
                user_msg,
                history_summary=ctx.history_summary,
                llm=self.llm,
                timeout=settings.LEAD_INTENT_TIMEOUT,
                degradation=degradation,
            )
            ctx.add_turn("user", user_msg)
            yield {
                "type": "intent",
                "intent": intent.intent.value,
                "confidence": intent.confidence,
                "needs_plan": intent.needs_plan,
                "degraded": intent.degraded,
            }

            # ── 2) Supervisor 循环：每轮决策 + 派发子任务，直到 stop / 轮次上限 ──
            available_datasources = await _load_available_datasources(
                db_session, str(ctx.user_id), ctx.datasource_id
            )
            max_rounds = getattr(settings, "LEAD_MAX_SUPERVISOR_ROUNDS", 4)
            for rnd in range(max_rounds):
                decision = await decide_action(
                    user_msg,
                    intent,
                    history_summary=ctx.digest(),
                    datasources=available_datasources,
                    subtask_summaries=ctx.turn_summaries,
                    llm=self.llm,
                    timeout=settings.LEAD_DECISION_TIMEOUT,
                    degradation=degradation,
                )
                yield {
                    "type": "decision",
                    "round": rnd,
                    "action": decision.action.value,
                    "tool": decision.tool_name,
                    "reason": decision.reason,
                    "degraded": decision.degraded,
                }

                # 反问：输出后本轮回结束，等用户下一轮消息带回目标
                if decision.action == ActionType.ASK_USER:
                    async for ev in self._emit_answer(decision.direct_text or "请补充更多信息，我好帮你继续。"):
                        yield ev
                    return
                # 主管收尾
                if decision.action == ActionType.STOP:
                    break
                # 降级防御：首轮降级按原兜底行为执行一次；非首轮降级意味着 LLM 已不可用，
                # 直接收尾（避免兜底路径在每轮重复触发分析，白白消耗预算）
                if decision.degraded and rnd > 0:
                    break
                if decision.action == ActionType.CALL_ANALYSIS:
                    async for ev in self._run_analysis_branch(
                        user_msg, decision, ctx, db_session, available_datasources,
                        round_idx=rnd, degradation=degradation,
                    ):
                        yield ev
                else:
                    async for ev in self._answer_branch(user_msg, decision, ctx):
                        yield ev

            # ── 3) 记忆回流 ──
            memory_event = await self._maybe_summarize(ctx)
            if memory_event:
                yield {"type": "memory_saved", "chars": len(memory_event["summary"])}
                yield {"type": "compressed_history", **memory_event}

            yield {"type": "done", "degraded": bool(degradation), "degradations": degradation}
        finally:
            if trace is not None:
                try:
                    trace.__exit__(None, None, None)
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
            return "canvas"
        return "orchestrator" if getattr(decision, "complexity", "complex") == "complex" else "react"

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
            await out_q.put(ev)

        async def _on_progress(p: StepProgress) -> None:
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
            if p.status == "fail" or settings.LEAD_PROGRESS_VERBOSE:
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
                        lead_ctx=ctx,
                        extra_plannable_tools=self.extra_plannable_tools,
                        on_progress=_on_progress,
                        memo=self._memo,
                        available_datasources=available_datasources,
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
        # 同时把结果摘要回写 turn_summaries，供 Supervisor 下一轮决策参考（避免重复执行）
        if result.success:
            blocks = result.blocks_added
            if blocks > 0:
                summary = f"分析完成，已在画布上添加了 {blocks} 个内容块，你可以直接查看和调整。"
            else:
                summary = "分析完成，结果已生成，请查看上方内容。"
            ctx.turn_summaries.append(f"[子任务完成] {summary}")
            yield {"type": "text", "content": summary}
        if result.report:
            yield {"type": "report", "content": result.report, "source": result.report_source}
            ctx.add_turn("assistant", result.report)
            ctx.turn_summaries.append(f"[报告摘要] {result.report[:240]}")
        elif result.error:
            yield {"type": "status", "message": f"本次分析未产出报告：{result.error}"}

    # ── 分支：直接回答（decision 已给文本则直出，否则轻量 LLM 生成）──
    async def _answer_branch(
        self,
        user_msg: str,
        decision: Decision,
        ctx: LeadContext,
    ) -> AsyncIterator[dict]:
        text = (decision.direct_text or "").strip()
        if text:
            async for ev in self._emit_answer(text):
                yield ev
            return
        messages = [{"role": "system", "content": _ANSWER_SYSTEM}]
        if ctx.history_summary.strip():
            messages.append({"role": "assistant", "content": f"【历史记忆】{ctx.history_summary}"})
        for t in ctx.turns[-8:]:
            if t.get("role") in ("user", "assistant"):
                messages.append({"role": t["role"], "content": str(t.get("content", ""))})
        messages.append({"role": "user", "content": user_msg})
        collected: list[str] = []
        try:
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

    # ── 记忆回流：把较早轮次折叠为摘要，交给 API 层持久化 ──
    async def _maybe_summarize(self, ctx: LeadContext) -> dict | None:
        """每积累 4 轮且总量 >= 6 轮时，把较早轮次压缩为摘要。

        返回 `{"summary":..., "covered_rounds":...}`，由 API 层 upsert 到 ai_memories。
        """
        total = len(ctx.turns)
        if total < 6 or total - ctx._summarized_upto < 4:
            return None
        older = ctx.turns[ctx._summarized_upto : total - 2]
        if not older:
            return None
        text = "\n".join(
            f"{'用户' if t.get('role') == 'user' else '助手'}: {str(t.get('content', ''))[:800]}"
            for t in older
        )[:6000]
        try:
            summary = await self.llm.complete(
                [
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {"role": "user", "content": text},
                ],
                temperature=0.2,
                max_tokens=300,
                enable_thinking=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[lead_agent] summarize_failed: {e}")
            return None
        summary = (summary or "").strip()
        if not summary:
            return None
        ctx._summarized_upto = total - 2
        ctx.turn_summaries.append(summary)
        return {"summary": summary, "covered_rounds": len(older)}


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
