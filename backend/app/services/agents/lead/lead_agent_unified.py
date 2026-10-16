# -*- coding: utf-8 -*-
"""P1 统一循环：一个 system + 工具层 + 循环闸（unified 模式）。

与 legacy 的差别（任务书 §2.1）：
  legacy ：请求 → merged（意图+决策）→ decision（后续轮）→ answer（组织文本）
           三个工位各看一份上下文，条件扇出 1~5 次 LLM 调用。
  unified：请求 → **一个** Lead Agent，在循环里「看 → 判 → 决」多步完成，
           角色认知只有一份，终止动作由模型显式给出（answer / ask_user / stop）。

**为什么单独一个模块**：不碰 LeadAgent.stream（legacy 路径），
unified 由 LEAD_AGENT_MODE=unified 显式开启，出问题一键回滚。

三条硬约束（§2.3 / §6）：
  1. **收敛闸在代码里**：步数上限 + 每类工具次数上限 + 强制终止动作，
     绝不依赖模型自觉；
  2. **写工具不在工具表里**（lead_agent_tools 已保证），本模块对未知工具名
     一律拒绝并记降级，而不是"尽力执行"；
  3. **每个新环节都有确定性降级**：观察失败 → 空结果继续；LLM 失败 → 确定性收尾。

依赖注入（不复制既有实现）：
  · emit_answer：LeadAgent._emit_answer（流式吐出确定性文本）
  · confirm_guard：LeadAgent._confirm_guard（HITL 三态机，原样复用）
  · 派发：直接复用 lead_dispatch.run_worker——子任务摘要/评审/画布刷新
    与 legacy 完全同一条路径（否则两套派发必然跑偏）
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Awaitable, Callable

from app.config import settings
from app.services.agents.lead.lead_agent_tools import (
    QUOTA,
    lead_tool_specs,
    tool_class,
)

logger = logging.getLogger("lvco.lead.unified")


def _render_datasources(datasources: list[dict]) -> str:
    """数据源清单渲染（id / 名称 / 类型 / 字段）。id 原样给出，禁止模型编造。"""
    lines: list[str] = []
    for ds in datasources:
        head = f"- id={ds.get('id')} 名称={ds.get('name')} 类型={ds.get('type')}"
        fields = ds.get("fields") or []
        if fields:
            names = [str(f.get("name") if isinstance(f, dict) else f) for f in fields][:40]
            head += " 字段=" + ",".join(names)
        lines.append(head)
    return "\n".join(lines)


def build_lead_messages(
    *,
    user_msg: str,
    ctx,
    available_datasources: list[dict] | None,
    canvas_layout: str = "",
    system_text: str = "",
) -> list[dict]:
    """把上下文拼成 messages（P1 版本）。

    P4 会把这一段搬进 TurnContextProvider.for_agent_loop（单出口装配）；
    先做成模块函数，便于单测与替换——这正是 P4 存在的理由。
    """
    from app.services.ai_prompts import LEAD_AGENT_SYSTEM

    system = system_text or LEAD_AGENT_SYSTEM
    blocks: list[str] = []
    if str(getattr(ctx, "history_summary", "") or "").strip():
        blocks.append("【历史记忆】\n" + ctx.history_summary.strip())
    if str(getattr(ctx, "metrics_ctx", "") or "").strip():
        blocks.append("【受治理指标清单】\n" + ctx.metrics_ctx.strip())
    if available_datasources:
        blocks.append("【可用数据源】\n" + _render_datasources(available_datasources))
    if canvas_layout.strip():
        blocks.append("【当前画布布局（已落盘，以此为准）】\n" + canvas_layout.strip())
    if getattr(ctx, "turn_summaries", None):
        blocks.append("【已完成子任务摘要 / 评审结论】\n" + "\n".join(ctx.turn_summaries))
    if blocks:
        system = system + "\n\n" + "\n\n".join(blocks)

    messages: list[dict] = [{"role": "system", "content": system}]
    for t in ctx.recent_turns(max_turns=12):
        role = t.get("role")
        if role in ("user", "assistant") and str(t.get("content") or "").strip():
            messages.append({"role": role, "content": t["content"]})
    messages.append({"role": "user", "content": user_msg})
    return messages


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def stream_unified(
    *,
    user_msg: str,
    ctx,
    db_session,
    llm,
    available_datasources: list[dict] | None = None,
    memo: dict | None = None,
    memo_lock=None,
    extra_plannable_tools=None,
    trace=None,
    degradation: list[str] | None = None,
    emit_answer: Callable[[str], AsyncIterator[dict]] | None = None,
    confirm_guard: Callable[..., Awaitable] | None = None,
    max_steps: int | None = None,
    emit_done: bool = True,
) -> AsyncIterator[dict]:
    """unified 模式的一轮：单 system + 工具循环 + 代码层收敛闸。"""
    degradation = degradation if degradation is not None else []
    limit = max_steps or int(getattr(settings, "LEAD_AGENT_MAX_STEPS", 6))
    tools = lead_tool_specs()
    remaining = dict(QUOTA)  # 每类工具剩余次数（观察 ≤2 / 派发 ≤3）

    async def _answer(text: str) -> AsyncIterator[dict]:
        if emit_answer is not None:
            async for ev in emit_answer(text):
                yield ev
        elif text:
            yield {"type": "report", "content": text, "source": "lead"}

    def _finish() -> dict:
        return {"type": "done", "degraded": bool(degradation), "degradations": degradation}

    # 画布快照：与 legacy 同一来源（请求级 CanvasStateProvider）
    canvas_layout = ""
    if getattr(ctx, "canvas_state", None) is not None:
        try:
            canvas_layout = await ctx.canvas_state.layout() or ""
        except Exception:  # noqa: BLE001
            canvas_layout = ""

    # ── 确认守卫（HITL）：命中即短路，不调 LLM（复用 legacy 的三态机）──
    if confirm_guard is not None:
        try:
            guard = await confirm_guard(ctx, user_msg)
        except Exception:  # noqa: BLE001
            logger.warning("[lead_unified] confirm_guard_failed", exc_info=True)
            guard = None
        if guard is not None:
            if guard[0] == "resume":
                from app.services.agents.lead.lead_decider import Decision  # noqa: F401
                from app.services.agents.lead.lead_dispatch import run_worker
                from app.services.agents.lead.lead_worker import resolve_worker

                decision = guard[1]
                worker = resolve_worker(ctx.entry, getattr(decision, "complexity", "complex"))
                async for ev in run_worker(
                    user_msg=user_msg, decision=decision, ctx=ctx, worker=worker,
                    db_session=db_session, available_datasources=available_datasources or [],
                    llm=llm, memo=memo, memo_lock=memo_lock,
                    extra_plannable_tools=extra_plannable_tools,
                    round_idx=0, degradation=degradation, trace=trace,
                ):
                    yield ev
                if emit_done:
                    yield _finish()
                return
            # cancel / 过期：确定性收尾
            async for ev in _answer(getattr(guard[1], "direct_text", "") or "好的，已取消。"):
                yield ev
            if emit_done:
                yield _finish()
            return

    ctx.add_turn("user", user_msg)
    messages = build_lead_messages(
        user_msg=user_msg, ctx=ctx,
        available_datasources=available_datasources, canvas_layout=canvas_layout,
    )

    finished = False
    for step in range(limit):
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        try:
            async for ev in llm.tool_call_round(
                messages, tools, temperature=0.3, streaming=False,
            ):
                et = ev.get("type")
                if et == "text":
                    text_parts.append(ev.get("content") or "")
                elif et == "tool_call":
                    tool_calls.append(ev)
        except Exception as e:  # noqa: BLE001
            # §6 不变量 4：LLM 不可用 → 确定性收尾（不空转、不重复派发）
            logger.warning("[lead_unified] llm_failed: %s", e)
            degradation.append("lead_agent_llm_failed")
            if "".join(text_parts).strip():
                async for ev in _answer("".join(text_parts)):
                    yield ev
            else:
                yield {"type": "error", "message": "服务暂时不可用，请稍后重试。"}
            break

        model_text = "".join(text_parts).strip()

        # 模型没调任何工具：有文本就当作回答（确定性降级），否则强制收尾
        if not tool_calls:
            if model_text:
                async for ev in _answer(model_text):
                    yield ev
            else:
                degradation.append("lead_agent_no_output")
                yield {"type": "status", "message": "本轮未能得出结论，已停止。"}
            finished = True
            break

        # 把 assistant 的 tool_calls 回写进 messages（OpenAI 协议要求）
        messages.append({
            "role": "assistant",
            "content": model_text or None,
            "tool_calls": [
                {"id": tc.get("id") or f"call_{step}_{i}", "type": "function",
                 "function": {"name": tc.get("name"), "arguments": json.dumps(
                     _parse_args(tc.get("arguments")), ensure_ascii=False)}}
                for i, tc in enumerate(tool_calls)
            ],
        })

        for i, tc in enumerate(tool_calls):
            name = str(tc.get("name") or "")
            args = _parse_args(tc.get("arguments"))
            call_id = tc.get("id") or f"call_{step}_{i}"
            cls = tool_class(name)

            # ── 终止动作：answer / ask_user / stop ──
            if cls == "terminal":
                if name == "answer":
                    async for ev in _answer(str(args.get("text") or model_text or "")):
                        yield ev
                elif name == "ask_user":
                    question = str(args.get("question") or "请补充更多信息，我好帮你继续。")
                    ask_kind = str(args.get("ask_kind") or "clarify")
                    try:
                        from app.services.lead_session_state import set_pending
                        set_pending(ctx.session_id, str(user_msg or ""))
                    except Exception:  # noqa: BLE001
                        logger.warning("[lead_unified] set_pending_failed", exc_info=True)
                    async for ev in _answer(question):
                        yield ev
                    if ask_kind == "confirm":
                        yield {"type": "confirm_request", "goal": str(user_msg or ""),
                               "question": question}
                else:  # stop
                    reason = str(args.get("reason") or "")
                    if reason:
                        logger.info("[lead_unified] stop reason=%s", reason)
                finished = True
                break

            # ── 未知工具（含写工具）：拒绝并记降级，绝不"尽力执行" ──
            if cls is None:
                degradation.append("lead_agent_unknown_tool")
                logger.warning("[lead_unified] rejected_tool name=%s", name)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": f"工具 {name} 不可用（不在你的工具表里）。"})
                continue

            # ── 配额闸（§2.3）──
            if remaining.get(cls, 0) <= 0:
                degradation.append(f"lead_agent_quota_{cls}")
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": f"{cls} 类工具本轮次数已用完，请直接收尾。"})
                continue
            remaining[cls] -= 1

            if cls == "observe":
                from app.services.agents.lead import lead_observe

                yield {"type": "tool_call", "name": name, "round": step, "args": args}
                try:
                    if name == "get_canvas_layout":
                        result = await lead_observe.get_canvas_layout(ctx)
                    elif name == "list_datasources":
                        result = await lead_observe.list_datasources(
                            db_session, str(ctx.user_id), datasource_id=ctx.datasource_id)
                    elif name == "list_fields":
                        result = await lead_observe.list_fields(
                            db_session, str(ctx.user_id), args.get("datasource_id"))
                    else:
                        # list_metrics / query_sample：P3 才接语义层与取数，P1 先确定性降级
                        degradation.append("lead_agent_observe_unimplemented_" + name)
                        result = []
                except Exception:  # noqa: BLE001
                    logger.warning("[lead_unified] observe_failed name=%s", name, exc_info=True)
                    degradation.append("lead_agent_observe_failed_" + name)
                    result = []
                payload = result if isinstance(result, str) else json.dumps(
                    result, ensure_ascii=False, default=str)
                yield {"type": "tool_result", "name": name, "ok": True, "result": payload[:4000]}
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": payload[:4000]})
                continue

            # ── 派发：复用 run_worker（子任务摘要/评审/画布刷新与 legacy 同路径）──
            from app.services.agents.lead.lead_decider import ActionType, Decision
            from app.services.agents.lead.lead_dispatch import run_worker
            from app.services.agents.lead.lead_worker import resolve_worker

            if name == "repair":
                goal = str(args.get("goal") or user_msg)
                guidance = str(args.get("gap") or "")
                complexity = "simple"          # §2.5：小缺口 → 轻量内核（react）
            else:
                goal = str(args.get("goal") or user_msg)
                guidance = ""
                complexity = str(args.get("complexity") or "complex")
                if complexity not in ("complex", "simple"):
                    complexity = "complex"     # 非法值 → 确定性兜底

            decision = Decision(
                action=ActionType.CALL_ANALYSIS,
                tool_name="run_analysis",
                tool_args={"goal": goal},
                reason="lead_agent:" + name,
                complexity=complexity,
                guidance=guidance,
            )
            worker = resolve_worker(ctx.entry, complexity)
            async for ev in run_worker(
                user_msg=user_msg, decision=decision, ctx=ctx, worker=worker,
                db_session=db_session, available_datasources=available_datasources or [],
                llm=llm, memo=memo, memo_lock=memo_lock,
                extra_plannable_tools=extra_plannable_tools,
                round_idx=step, degradation=degradation, trace=trace,
            ):
                yield ev
            # 派发后刷新画布快照，供下一步观察/判断
            if getattr(ctx, "canvas_state", None) is not None:
                try:
                    canvas_layout = await ctx.canvas_state.layout(force=True) or canvas_layout
                except Exception:  # noqa: BLE001
                    pass
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": "已派发子任务，执行摘要见上文。"})
        if finished:
            break
    else:
        # 走满步数仍无终止动作 → 代码补 stop（§2.3）
        degradation.append("lead_agent_step_limit")
        yield {"type": "status",
               "message": f"本轮已连续执行 {limit} 步仍未收尾，已停止；如需继续请再说。"}

    # emit_done=False 时把 done 交给调用方：后处理（收尾总结 / 记忆回流）
    # 必须插在 done **之前**，否则前端会在记忆事件到达前就解锁输入。
    if emit_done:
        yield _finish()
