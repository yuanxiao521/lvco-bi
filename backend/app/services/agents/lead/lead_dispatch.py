# -*- coding: utf-8 -*-
"""派发：把一轮子任务交给选定的 worker 执行，回收结果并写回验收记录。

从 `LeadAgent._run_analysis_branch` 抽出（原为实例方法，与主循环混在一起）。抽出的意义：

1. **依赖显式化**——llm / memo / 锁 / 工具白名单不再是实例隐式状态，而是调用方传入，
   "这个函数到底需要什么"一眼可见；
2. **职责单一**——只负责「派发 + 事件转发 + 结果 emit + 写验收记录」；
   选哪个 worker 由 `lead_worker.resolve_worker` 决定，验收口径由 `lead_review` 提供。

`worker.mode` 直接进 `RunAnalysisArgs.constraints`，对下游 `run_analysis` 而言与改造前等价。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, AsyncIterator

from app.services.agents.lead.lead_perception import StepProgress, render_progress_text
from app.services.agents.lead.lead_review import (
    build_execution_summary,
    compose_result_text,
    review_subtask,
)
from app.services.agents.lead.lead_tools import RunAnalysisArgs, RunAnalysisResult, run_analysis
from app.services.agents.lead.lead_worker import WorkerSpec

if TYPE_CHECKING:  # 仅类型标注用：避免与 lead_agent 形成运行时循环依赖
    from app.services.agents.lead.lead_agent import LeadContext
    from app.services.agents.lead.lead_decider import Decision

logger = logging.getLogger("lvco.lead.dispatch")


async def dispatch_analysis(
    *,
    goal: str,
    ctx: "LeadContext",
    worker: WorkerSpec,
    constraints: dict | None = None,
    db_session,
    available_datasources: list[dict],
    llm,
    memo: dict | None,
    memo_lock,
    extra_plannable_tools,
    emit=None,
    on_progress=None,
    trace=None,
    guidance: str = "",
) -> RunAnalysisResult:
    """派发的**内核**：组装 RunAnalysisArgs → 在 memo_lock 下执行 run_analysis。

    P0 抽出的「能力接口」。为什么必须只有一份：run_worker 是流式路径（派发 + 事件
    转发 + emit + 写验收记录），P1/P2 会把"派发"注册成 Lead 的工具——如果工具层
    自己再写一遍派发，就会出现"工具派发"和"流式派发"两套实现，迟早跑偏
    （本项目已经在事件表上踩过一次同样的坑）。

    形状刻意做成 goal + 内核进、RunAnalysisResult 出：
    P1 的工具层要的正是这个（不需要事件流那套 plumbing）。

    行为与抽出前逐字节一致——constraints 的合并方式、memo_lock 的持有时机、
    传给 run_analysis 的参数全部原样保留。
    """
    args = RunAnalysisArgs(
        goal=goal,
        datasource_id=ctx.datasource_id,
        canvas_id=ctx.canvas_id,
        entry=ctx.entry,
        constraints={"mode": worker.mode, **(constraints or {})},
    )
    async with memo_lock:
        return await run_analysis(
            args,
            db_session=db_session,
            emit=emit,
            llm=llm,
            trace=trace,
            lead_ctx=ctx,
            extra_plannable_tools=extra_plannable_tools,
            on_progress=on_progress,
            memo=memo,
            available_datasources=available_datasources,
            worker_guidance=guidance,
        )


async def run_worker(
    *,
    user_msg: str,
    decision: Decision,
    ctx: LeadContext,
    worker: WorkerSpec,
    db_session,
    available_datasources: list[dict],
    llm,
    memo: dict | None,
    memo_lock,
    extra_plannable_tools,
    round_idx: int = 0,
    degradation: list[str] | None = None,
    trace=None,
    do_review: bool = True,
    on_result=None,
) -> AsyncIterator[dict]:
    tool_args = decision.tool_args or {}
    goal = str(tool_args.get("goal") or user_msg)
    constraints = dict(tool_args.get("constraints") or {})
    mode = worker.mode
    # args 的组装已下沉到 dispatch_analysis（派发内核），此处不再重复一份
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
            holder["result"] = await dispatch_analysis(
                goal=goal,
                ctx=ctx,
                worker=worker,
                constraints=constraints,
                db_session=db_session,
                available_datasources=available_datasources,
                llm=llm,
                memo=memo,
                memo_lock=memo_lock,
                extra_plannable_tools=extra_plannable_tools,
                emit=_forward,
                on_progress=_on_progress,
                trace=trace,
                guidance=getattr(decision, "guidance", "") or "",
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
    # P2：把结果交给调用方（unified 循环要自己跑"评审阶段"——它是固定工序，
    # 由代码触发；legacy 路径用 do_review=True 保持原行为不变）
    if result is not None and on_result is not None:
        on_result(result)
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
    # do_review=False：调用方（unified 循环）会跑完整的**评审阶段**
    # （HARD 硬闸 + 仅 SOFT 非空时裁量），这里就不重复评一遍——
    # 否则同一次派发会写两条 [子任务评估]，两条实现也会跑偏。
    if do_review:
        assessment = review_subtask(result, entry=ctx.entry, user_goal=goal)
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
        ctx.turn_summaries.append(build_execution_summary(result, canvas_layout=canvas_now))
        summary = compose_result_text(blocks=blocks, canvas_highlight=canvas_highlight)
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


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
