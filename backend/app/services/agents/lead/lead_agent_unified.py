# -*- coding: utf-8 -*-
"""P1 单 Agent 循环：一个 system + 工具层 + 循环闸（single_agent 模式）。

与 legacy 的差别（任务书 §2.1）：
  legacy ：请求 → merged（意图+决策）→ decision（后续轮）→ answer（组织文本）
           三个工位各看一份上下文，条件扇出 1~5 次 LLM 调用。
  single_agent：请求 → **一个** Lead Agent，在循环里「看 → 判 → 决」多步完成，
           角色认知只有一份，终止动作由模型显式给出（answer / ask_user / stop）。

**为什么单独一个模块**：不碰 LeadAgent.stream（legacy 路径），
由 LEAD_AGENT_MODE=single_agent 显式开启，出问题一键回滚。

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
    decision_action,
    lead_tool_specs,
    tool_class,
)
from app.services.agents.lead.lead_intent_signal import derive_intent, normalize_intent

logger = logging.getLogger("lvco.lead.unified")


# 上下文装配已**收归** TurnContextProvider.for_agent_loop（P4 装配统一）：
# 原先这里的 build_lead_messages / _render_datasources 是 P1 的临时实现，
# 现由 provider（Source 出块）+ assembler（排序/渲染/台账）承担，本模块不再拼上下文。


# 多产物 / 多步的文本特征：模型判 simple、但目标明显不止一步时，抬回 complex。
_MULTI_STEP_MARKS = (
    "报告", "看板", "对比", "分别", "多个", "多张", "趋势", "同比", "环比",
    "分析", "图表", "柱状图", "折线图", "饼图", "散点", "漏斗", "热力",
)


def effective_complexity(
    goal: str, model_value: str, *, entry: str = "chat"
) -> tuple[str, bool]:
    """complexity 的确定性兜底：模型偏保守时把 simple 抬回 complex。

    为什么需要（实测依据）：双跑对拍显示单 Agent 循环把 63% 的派发路由到 react
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


# 破坏性操作词：命中即**必须**走确认卡片（宁可多问一次，也不能让"清空画布"直接执行）。
# 与 legacy 的 _CLEAR_MARKERS 同源、方向相反：那边用它"抑制无效澄清"，这边用它"强制确认"。
_ASK_CONFIRM_MARKERS: tuple[str, ...] = (
    "清空", "清掉", "清空画布", "删除所有", "删掉所有", "全部删除", "移除所有",
    "不可恢复", "clear canvas", "remove all", "delete all",
)


def effective_ask_kind(ask_kind: str, question: str) -> tuple[str, bool]:
    """ask_user 的 ask_kind 确定性兜底：破坏性操作一律升级成 confirm。

    为什么需要（真机实测）：模型对"把画布清空"会调 ask_user，但**漏填 ask_kind**
    （schema 里它是可选字段），于是 ask_kind 退化成 clarify → 前端不渲染确认卡片，
    用户在聊天框里看到一句反问，破坏性操作却可能被后续文本"确认"直接触发。
    legacy 靠 prompt 强制输出该字段；单 Agent 这里再补一道**代码闸**（§6 不变量 7：
    HITL 不退化）——prompt 会失灵，代码闸不会。

    返回 (最终 ask_kind, 是否发生了升级)。
    """
    kind = ask_kind if ask_kind in ("confirm", "clarify") else "clarify"
    if kind == "confirm":
        return kind, False
    if any(marker in (question or "") for marker in _ASK_CONFIRM_MARKERS):
        return "confirm", True
    return kind, False


def explicit_kernel(name: str) -> str | None:
    """派发工具 → 显式执行内核（复用 legacy 的同一张映射表，不另立词汇）。

    legacy 的内核选择来自 `REPAIR_KIND_KERNEL[repair_kind]`
    （patch→react / redo→orchestrator，lead_agent.py:649-658）；统一循环里没有
    repair_kind 字段，repair 工具在语义上就是 patch（§2.5：小缺口 → 轻量内核）。
    这里**显式**取表，而不是靠 complexity="simple" 间接落到 react——
    否则哪天表改了，两处会静默漂移，而漂移表现只是"某个场景变慢/变贵"。

    redo 没有对应：循环用「同一 goal 再派」当启发式，只用于 bypass_memo
    （跳过缓存），**不**据此换内核。是否给 repair 工具加 kind 参数（patch/redo
    由模型判断）属于工具面设计，需要单独一轮 + 评测。
    """
    from app.services.agents.lead.lead_agent_tools import REPAIR_KIND_KERNEL

    if name == "repair":
        return REPAIR_KIND_KERNEL.get("patch")
    return None


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
    """single_agent 模式的一轮：单 system + 工具循环 + 代码层收敛闸。"""
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
            # cancel / 过期：确定性收尾。guard[1] 是 str（守卫返回的提示文本）——
            # 历史 bug：按 direct_text 属性取值对 str 恒为空 → 所有提示被吞成兜底"好的，已取消。"，
            # "确认已过期"与"已取消"用户无法区分。
            _guard_text = guard[1] if isinstance(guard[1], str) else str(getattr(guard[1], "direct_text", "") or "")
            async for ev in _answer(_guard_text or "好的，已取消。"):
                yield ev
            if emit_done:
                yield _finish()
            return

    # ── P4：装配统一（唯一出口）─────────────────────────────────────────────
    # 上下文**只由 provider 装配**：循环每步向它要一份完整 messages，调用方不碰
    # 消息结构（tool_call_id 配对、块顺序、台账都在 provider/assembler 一处）。理由：
    #   · LLM 无状态，每步本来就要重发完整 messages，"重建"不增加任何 token；
    #   · 请求级事实（窗口/长期记忆/指标/数据源/产物）由 Source 恒定持有，不重读；
    #   · 轮内会变的事实必须**每步重读**（与 legacy 第 2 轮起的做法一致）：
    #     画布在派发落块后 force 重读并 refresh 回 provider；子任务摘要/评估靠
    #     SubtaskSource 持有的**活引用**自动跟随 ctx.turn_summaries。
    #     （legacy 也是这么接的：`subtask_summaries=ctx.turn_summaries` 不拷贝，
    #      画布每轮 `layout(force=True)` 后 `refresh_canvas`。）
    # 构造必须在 ctx.add_turn 之前（否则本轮用户消息会在窗口里出现两次）。
    from app.services.ai_prompts import LEAD_AGENT_SYSTEM
    from app.services.context.provider import TurnContextProvider, TurnInputs

    provider = TurnContextProvider.for_turn(TurnInputs(
        user_id=str(ctx.user_id),
        session_id=ctx.session_id or "",
        entry=ctx.entry,
        turns=list(ctx.turns),
        history_summary=ctx.history_summary,
        # 必须传**活引用**（不拷贝）：派发会在 run_worker 里 append 摘要/评估，
        # SubtaskSource/AssessmentSource 持有同一列表 → 每步重建时自然带上最新事实。
        # 拷一份的话，本轮的评估永远进不了决策上下文（legacy 第 508 行就是不拷贝）。
        subtask_summaries=ctx.turn_summaries,
        datasources=available_datasources,
        metrics_ctx=ctx.metrics_ctx,
        canvas_layout=canvas_layout,
        artifact_note=str(getattr(ctx, "extra_context", "") or ""),
    ))
    assembled = None   # 循环结束后统一记账（见末尾）
    ctx.add_turn("user", user_msg)

    finished = False
    seen_goals: set[str] = set()   # 本请求内派发过的 goal（用于识别"重做"）
    # 理解层标签（intent）：请求级一次（C+B 合流）。分层见 lead_intent_signal 模块头——
    #   intent = 用户这一轮要什么（1 条）；decision = 这一步做什么、为什么（N 条）。
    # intent **不参与路由**（路由仍由工具选择 + effective_complexity 决定），
    # 它是标签：展示给用户 + 监控"理解漂移"。
    intent_emitted = False
    intent_value = ""
    for step in range(limit):
        # 每步都向 provider 要**完整** messages：循环不手工维护消息列表
        assembled = provider.for_agent_loop(user_msg, system_text=LEAD_AGENT_SYSTEM)
        messages = assembled.messages
        step_msgs: list[dict] = []      # 本步新增消息，步末提交给 provider
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
        step_msgs.append({
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

            # ── 理解层标签（请求级一次，早于该步的 decision 事件）──
            # 来源两种，都是真实判断，不回填模板话术：
            #   · model  ：模型在决策类工具参数里自述（method a，与 reason 同源）
            #   · derived：模型没给 → 按 动作+入口+复杂度 推导，标记为"推断"
            # 观察类不发：它只是"看一眼"，不表达本轮目标（与 decision 不发同理）。
            if cls is not None and cls != "observe":
                model_intent = normalize_intent(args.get("intent"))
                if not intent_emitted:
                    if model_intent:
                        intent_value, intent_source = model_intent, "model"
                    else:
                        _goal = str(args.get("goal") or args.get("text") or user_msg or "")
                        _cx = ""
                        if cls == "repair":
                            _cx = "simple"      # patch 一律走轻量内核
                        elif cls == "dispatch":
                            _cx = effective_complexity(
                                _goal, str(args.get("complexity") or ""), entry=ctx.entry
                            )[0]
                        intent_value = derive_intent(
                            entry=ctx.entry,
                            action=decision_action(name),
                            complexity=_cx,
                            dispatched=cls in ("dispatch", "repair"),
                        )
                        intent_source = "derived"
                    intent_emitted = True
                    yield {"type": "intent", "intent": intent_value,
                           "confidence": None, "source": intent_source, "step": step,
                           "degraded": intent_source == "derived"}
                elif model_intent and model_intent != intent_value:
                    # 多步里改主意：标签定稿后不再改（避免同轮两条自相矛盾），
                    # 但记降级——这是"理解漂移"的唯一可观测信号，别静默吞掉。
                    degradation.append("lead_intent_drift")

            # ── 汇报层契约（与 legacy 对齐）：决策点发一条 decision 事件 ──
            # legacy 每轮决策都 emit decision(action/tool/reason)，前端 agentMeta 用它
            # 显示"这一步做什么、为什么"。统一循环没有独立的决策调用，因此把**工具调用**
            # 当决策点：reason 由模型在工具参数里自述（method a：真实理由，不回填模板话术）。
            # 观察类不发 decision——它只是"看一眼"，legacy 也没有对应动作，前端用
            # tool_call/tool_result 已经能看到；未知工具同样不发（只记降级）。
            if cls is not None and cls != "observe":
                yield {"type": "decision", "round": step,
                       "action": decision_action(name), "tool": name,
                       "reason": str(args.get("reason") or ""), "degraded": False}

            # ── 终止动作：answer / ask_user / stop ──
            if cls == "terminal":
                if name == "answer":
                    async for ev in _answer(str(args.get("text") or model_text or "")):
                        yield ev
                elif name == "ask_user":
                    question = str(args.get("question") or "请补充更多信息，我好帮你继续。")
                    ask_kind, _ask_upgraded = effective_ask_kind(
                        str(args.get("ask_kind") or "clarify"), question
                    )
                    if _ask_upgraded:
                        # 模型漏填/填错 ask_kind 但问题涉及破坏性操作 → 强制升级为确认卡片
                        degradation.append("lead_ask_kind_upgraded_to_confirm")
                        logger.info(
                            "[lead_unified] ask_kind_upgraded_to_confirm question=%s",
                            question[:40],
                        )
                    try:
                        from app.services.lead_session_state import lead_session_state
                        lead_session_state.set_pending(ctx.session_id, str(user_msg or ""))
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
                step_msgs.append({"role": "tool", "tool_call_id": call_id,
                                   "content": f"工具 {name} 不可用（不在你的工具表里）。"})
                continue

            # ── 配额闸（§2.3）──
            if remaining.get(cls, 0) <= 0:
                degradation.append(f"lead_agent_quota_{cls}")
                step_msgs.append({"role": "tool", "tool_call_id": call_id,
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
                step_msgs.append({"role": "tool", "tool_call_id": call_id,
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
            worker = resolve_worker(ctx.entry, complexity, explicit_kernel(name))
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
            # 派发后刷新画布快照：后续步骤的上下文与评审输入都要用最新布局。
            # 与 legacy 第 2 轮起的做法完全一致（layout(force=True) → refresh_canvas）：
            # 画布块是**独立的一等上下文**，不能只靠工具结果里的执行摘要转述。
            if getattr(ctx, "canvas_state", None) is not None:
                try:
                    canvas_layout = await ctx.canvas_state.layout(force=True) or canvas_layout
                except Exception:  # noqa: BLE001
                    pass
                provider.refresh_canvas(canvas_layout)

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
            step_msgs.append({"role": "tool", "tool_call_id": call_id, "content": review_note})
        # 本步新增消息（assistant 的 tool_calls + 各 role=tool 结果）提交给 provider；
        # 下一步重建 messages 时自然带上它们——循环里的"增量"只有这一个来源。
        provider.add_tool_result(step_msgs)
        if finished:
            break
    else:
        # 走满步数仍无终止动作 → 代码补 stop（§2.3）
        degradation.append("lead_agent_step_limit")
        yield {"type": "status",
               "message": f"本轮已连续执行 {limit} 步仍未收尾，已停止；如需继续请再说。"}

    # 台账：装配覆盖**整个循环**（system + 窗口 + 每步工具结果），落 trace 供核 token 结构。
    # 放在循环之后而不是首步之前——首步台账看不到工具结果，会低估真实上下文规模。
    if assembled is not None:
        logger.info("[lead_unified] ledger %s", assembled.ledger.summary())
        if trace is not None:
            try:
                trace.update(metadata={"agent_loop_ledger": assembled.ledger.as_dict()})
            except Exception:  # noqa: BLE001
                pass

    # emit_done=False 时把 done 交给调用方：后处理（收尾总结 / 记忆回流）
    # 必须插在 done **之前**，否则前端会在记忆事件到达前就解锁输入。
    if emit_done:
        yield _finish()
