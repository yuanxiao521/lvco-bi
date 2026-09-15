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
    metrics_ctx: str = ""                     # 受治理指标清单（入口注入，answer/决策复用）
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
            canvas_layout = ""
            if ctx.entry == "canvas" and ctx.canvas_id:
                canvas_layout = await _load_canvas_snapshot(
                    db_session, str(ctx.user_id), ctx.canvas_id
                )
            canvas_layout = canvas_layout or ""
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
                    # 每轮决策前刷新画布快照：上一轮子任务落块后，DB 里的画布已变化
                    if ctx.entry == "canvas" and ctx.canvas_id:
                        canvas_layout = await _load_canvas_snapshot(
                            db_session, str(ctx.user_id), ctx.canvas_id
                        ) or canvas_layout
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
                    async for ev in self._answer_branch(user_msg, decision, ctx, trace=trace):
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

            # ── 3) 记忆回流 ──
            memory_event = await self._maybe_summarize(ctx, trace=trace)
            if memory_event:
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
        if result.success and ctx.entry == "canvas" and ctx.canvas_id:
            try:
                canvas_highlight = await _load_canvas_narrative(
                    db_session, str(ctx.user_id), ctx.canvas_id
                ) or ""
            except Exception:  # noqa: BLE001
                canvas_highlight = ""
        if result.success:
            blocks = result.blocks_added
            # 落块后前端已实时保存到 DB，刷新画布快照让 Supervisor 看到"落的到底是什么"
            canvas_now = ""
            if ctx.entry == "canvas" and ctx.canvas_id:
                canvas_now = await _load_canvas_snapshot(
                    db_session, str(ctx.user_id), ctx.canvas_id
                ) or ""
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
        trace=None,
    ) -> AsyncIterator[dict]:
        text = (decision.direct_text or "").strip()
        if text:
            async for ev in self._emit_answer(text):
                yield ev
            return
        messages = [{"role": "system", "content": _ANSWER_SYSTEM}]
        if ctx.history_summary.strip():
            messages.append({"role": "assistant", "content": f"【历史记忆】{ctx.history_summary}"})
        if ctx.metrics_ctx.strip():
            messages.append({"role": "assistant", "content": f"【受治理指标清单】\n{ctx.metrics_ctx}"})
        for t in ctx.turns[-8:]:
            if t.get("role") in ("user", "assistant"):
                messages.append({"role": t["role"], "content": str(t.get("content", ""))})
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

    # ── 记忆回流：把较早轮次折叠为摘要，交给 API 层持久化 ──
    async def _maybe_summarize(self, ctx: LeadContext, trace=None) -> dict | None:
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
            if trace is not None:
                from app.services.observability import observe_llm_call

                with observe_llm_call(trace, "lead_memory_summary",
                                      messages=[{"role": "system", "content": _SUMMARY_SYSTEM},
                                                {"role": "user", "content": text}]) as span:
                    result = await self.llm.complete(
                        [
                            {"role": "system", "content": _SUMMARY_SYSTEM},
                            {"role": "user", "content": text},
                        ],
                        temperature=0.2,
                        max_tokens=300,
                        enable_thinking=False,
                        return_usage=True,
                    )
                    summary, usage_meta = result if isinstance(result, tuple) else (result, None)
                    if usage_meta:
                        span.update(usage=usage_meta)
            else:
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
