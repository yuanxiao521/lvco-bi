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


# 多产物 / 多步的文本特征：模型判 simple、但目标明显不止一步时，抬回 complex。
_MULTI_STEP_MARKS = (
    "报告", "看板", "对比", "分别", "多个", "多张", "趋势", "同比", "环比",
    "分析", "图表", "柱状图", "折线图", "饼图", "散点", "漏斗", "热力",
)


def effective_complexity(
    goal: str, model_value: str, *, entry: str = "chat"
) -> tuple[str, bool]:
    """complexity 的确定性兜底：模型偏保守时把 simple 抬回 complex。

    为什么需要（实测依据）：双跑对拍显示 unified 把 63% 的派发路由到 react
    （legacy 只有 37%），而 react 的工具轮更费 LLM 调用——20 题多出 47 次（+32%）。
    根因是模型倾向选 simple。判据与 legacy 的 `_guess_complexity` 同源
    （"多步 / 多产物 → complex"），只是这里没有 intent，改用 goal 文本特征。

    返回 (complexity, overridden)；overridden=True 表示**推翻了模型的选择**，
    调用方应记 degraded（便于观测这个兜底到底触发了多少次）。
    """
    if model_value not in ("complex", "simple"):
        # 缺失 / 非法 → 走默认 complex（与 legacy 的确定性兜底一致），不算推翻模型
        return "complex", False
    if model_value == "complex":
        return "complex", False
    if any(m in (goal or "") for m in _MULTI_STEP_MARKS):
        return "complex", True
    return "simple", False


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
    # 循环内的对话历史（assistant 的 tool_calls + 各工具结果）。
    # messages 每步**重建**：上下文块（画布快照 / 子任务摘要 / 评审结论）在循环里会变，
    # 只建一次的话，模型看不到"落块后画布变成什么样""上轮评审说缺什么"。
    history: list[dict] = []

    def _messages() -> list[dict]:
        return build_lead_messages(
            user_msg=user_msg, ctx=ctx,
            available_datasources=available_datasources, canvas_layout=canvas_layout,
        ) + history

    finished = False
    seen_goals: set[str] = set()   # 本请求内派发过的 goal（用于识别"重做"）
    for step in range(limit):
        messages = _messages()
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

        # 把 assistant 的 tool_calls 回写进 history（OpenAI 协议要求）
        history.append({
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
                history.append({"role": "tool", "tool_call_id": call_id,
                                "content": f"工具 {name} 不可用（不在你的工具表里）。"})
                continue

            # ── 配额闸（§2.3）──
            if remaining.get(cls, 0) <= 0:
                degradation.append(f"lead_agent_quota_{cls}")
                history.append({"role": "tool", "tool_call_id": call_id,
                                "content": f"{cls} 类工具本轮次数已用完，请直接收尾。"})
                continue
            remaining[cls] -= 1

            if cls == "observe":
                from app.services.agents.lead import lead_observe

                yield {"type": "tool_call", "name": name, "round": step, "args": args}
                # P3：观察执行收敛到 lead_observe.recon_tool —— 独立 trace 名 lead_recon、
                # 失败即降级、未接的工具返回 None（由调用方记降级，绝不假装成功）
                result = await lead_observe.recon_tool(
                    name, ctx=ctx, args=args, db_session=db_session,
                    trace=trace, degradation=degradation,
                )
                if result is None:
                    payload, ok = f"（{name} 暂时不可用，请按现有信息判断）", False
                else:
                    payload = result if isinstance(result, str) else json.dumps(
                        result, ensure_ascii=False, default=str)
                    ok = True
                yield {"type": "tool_result", "name": name, "ok": ok, "result": payload[:4000]}
                history.append({"role": "tool", "tool_call_id": call_id,
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
                # 模型给的 complexity 会被**确定性兜底**复核（见 effective_complexity）：
                # 实测模型偏保守，会把明显多步的目标判成 simple，导致反复走轻量内核。
                complexity, overridden = effective_complexity(
                    goal, str(args.get("complexity") or ""), entry=ctx.entry
                )
                if overridden:
                    degradation.append("lead_agent_complexity_override")

            # ── P2.5.1：同一个 goal 在本请求内**再派一次** = 重做（redo）──
            # 必须显式跳过 memo，否则会命中上一轮的缓存、直接返回"未达标"的旧结果，
            # 日志里只显示 memo_hit——看起来像"执行过了"，实际什么都没跑。
            already_dispatched = goal in seen_goals
            seen_goals.add(goal)

            decision = Decision(
                action=ActionType.CALL_ANALYSIS,
                tool_name="run_analysis",
                tool_args={"goal": goal},
                reason="lead_agent:" + name,
                complexity=complexity,
                guidance=guidance,
            )
            worker = resolve_worker(ctx.entry, complexity)
            captured: dict = {}   # 接住 RunAnalysisResult，供随后的评审阶段使用
            async for ev in run_worker(
                user_msg=user_msg, decision=decision, ctx=ctx, worker=worker,
                db_session=db_session, available_datasources=available_datasources or [],
                llm=llm, memo=memo, memo_lock=memo_lock,
                extra_plannable_tools=extra_plannable_tools,
                round_idx=step, degradation=degradation, trace=trace,
                # 评审交给下面的"评审阶段"统一做（do_review=False），
                # 否则同一次派发会写两条 [子任务评估]
                do_review=False,
                on_result=lambda r: captured.setdefault("result", r),
                # 重做（同 goal 再派）或 repair：显式跳过 memo。
                # 与"guidance 进幂等键"互补——那条治"带指导的重派"，
                # 这条治"goal 一字不改的重做"。
                bypass_memo=(already_dispatched or name == "repair"),
            ):
                yield ev
            # 派发后刷新画布快照：后续步骤的上下文与评审输入都要用最新布局
            if getattr(ctx, "canvas_state", None) is not None:
                try:
                    canvas_layout = await ctx.canvas_state.layout(force=True) or canvas_layout
                except Exception:  # noqa: BLE001
                    pass

            # ── P2 评审阶段：固定执行、由代码触发（不是模型按需调用的工具）──
            #   · HARD 非空 → 硬闸直接 fail，不花 LLM（也绝不允许被翻案）
            #   · 仅 SOFT 非空 → 一次裁量；裁量失败退回纯代码结论
            # 结论写 turn_summaries（沿用 [子任务评估] 前缀，零通道改动），
            # 并作为**本次派发的工具结果**回给模型——它靠这个决定 repair / 重派 / 收尾。
            result = captured.get("result")
            review_note = "已派发子任务，执行摘要见上文。"
            if result is not None:
                from app.services.agents.lead.lead_review import build_execution_summary
                from app.services.agents.lead.lead_review_stage import run_review_stage

                exec_summary = build_execution_summary(result, canvas_layout=canvas_layout)
                # P3：把 Lead **自己声明**的验收标准交给评审阶段核验（声明缺失则跳过）
                _accept = args.get("acceptance")
                outcome = await run_review_stage(
                    result, entry=ctx.entry, user_goal=goal, llm=llm,
                    execution_summary=exec_summary, trace=trace, degradation=degradation,
                    acceptance=_accept if isinstance(_accept, dict) else None,
                )
                ctx.turn_summaries.append(outcome.text)
                if outcome.passed:
                    review_note = (
                        "子任务评审通过。已满足用户诉求就收尾（answer / stop），"
                        "否则继续下一步。"
                    )
                else:
                    degradation.append("lead_review_fail")
                    review_note = "子任务评审未通过：" + (outcome.reason or "存在缺口")
                    if outcome.guidance:
                        review_note += "。改进指导：" + outcome.guidance
                    review_note += "。小缺口用 repair 补；方向错或执行失败则重新 dispatch_analysis。"
            history.append({"role": "tool", "tool_call_id": call_id, "content": review_note})
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
