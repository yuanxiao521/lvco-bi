"""阶段 5 兜底验证（对齐设计 §9）：

- 兜底：注入 LLM 超时 → 主导 Agent 仍产出报告，且 `done.degraded=true`。

与 `test_lead_phase4_fallback.py` 的区别：那份测的是 API 层护栏（异常回落），
这份测的是 **LeadAgent 内部**——意图/决策 LLM 超时后走确定性兜底，
但分析分支照常执行并给出报告（用户始终有输出）。
"""
import json

import pytest

from app.services.agents.lead.lead_agent import LeadAgent, LeadContext
from app.services.agents.lead.lead_tools import RunAnalysisResult


class _IntentOkDecisionTimeoutLLM:
    """首轮合并调用（意图+决策一次）：第 1 次 complete 返回合法 JSON（含 intent 但缺 action，
    决策解析降级为确定性兜底）；`fail_from>=2` 时第 2 次调用（后续轮次决策）抛超时。

    `stream_chat` 供纯回答分支兜底使用。
    """

    def __init__(self, intent_obj: dict, *, fail_from: int = 2) -> None:
        self._intent_obj = intent_obj
        self._fail_from = fail_from
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        if self.complete_calls >= self._fail_from:
            raise TimeoutError("simulated llm timeout")
        return json.dumps(self._intent_obj, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield "兜底回答"


@pytest.fixture
def fake_run_analysis(monkeypatch):
    """把 run_analysis 替换为确定性实现：产出一段报告，不碰真实编排器。"""
    calls = {"n": 0}

    async def _fake(args, *, emit, **kw):  # noqa: ANN001
        calls["n"] += 1
        await emit({"type": "status", "message": "开始分析"})
        await emit({"type": "tool_call", "name": "query_sql", "args": {"sql": "select 1"}})
        await emit({"type": "text", "content": "报告：总销售额 100 元。"})
        return RunAnalysisResult(
            success=True,
            report="报告：总销售额 100 元。",
            steps=[{"step_id": 1, "goal": "查数"}],
            blocks_added=1,
            report_source="orchestrator",
            elapsed_ms=12,
        )

    import app.services.agents.lead.lead_agent as mod

    monkeypatch.setattr(mod, "run_analysis", _fake)
    return calls


async def test_decision_timeout_still_reports_and_flags_degraded(fake_run_analysis):
    """决策 LLM 超时 → 按 needs_plan 兜底为 call_analysis → 仍出报告 + done.degraded。"""
    llm = _IntentOkDecisionTimeoutLLM(
        {"intent": "analysis", "confidence": 0.9, "needs_plan": True, "reason": "多步"}
    )
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")

    events = [ev async for ev in agent.stream("帮我做一份完整的销售分析", ctx=ctx, db_session=None)]
    types = [e["type"] for e in events]

    assert "intent" in types and "decision" in types

    intent_ev = next(e for e in events if e["type"] == "intent")
    assert intent_ev["intent"] == "analysis"
    assert intent_ev["degraded"] is False

    decision_ev = next(e for e in events if e["type"] == "decision")
    assert decision_ev["action"] == "call_analysis"
    assert decision_ev["degraded"] is True

    reports = [e for e in events if e["type"] == "report"]
    assert reports and "总销售额" in reports[-1]["content"]

    done = events[-1]
    assert done["type"] == "done"
    assert done["degraded"] is True
    assert "lead_decision_fallback" in done["degradations"]
    assert fake_run_analysis["n"] == 1


async def test_intent_timeout_falls_back_to_rule(fake_run_analysis):
    """意图 LLM 也超时 → 规则意图兜底；非分析类问题走回答分支，同样有输出。"""
    llm = _IntentOkDecisionTimeoutLLM({}, fail_from=1)  # 第 1 次即失败
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s2")

    events = [ev async for ev in agent.stream("你好，介绍一下你自己", ctx=ctx, db_session=None)]
    done = events[-1]

    intent_ev = next(e for e in events if e["type"] == "intent")
    assert intent_ev["degraded"] is True
    assert done["type"] == "done"
    assert done["degraded"] is True
    assert "lead_intent_fallback" in done["degradations"]
    # 有任意文本/报告输出，用户不会看到空白
    assert any(e["type"] in ("text", "report") for e in events)


class _MergedAnswerThenStopLLM:
    """合并调用返回 answer，第二轮决策返回 stop（方案 B：prompt 硬规则收敛）。"""

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        if self.complete_calls == 1:
            return json.dumps({
                "intent": "chat",
                "confidence": 0.9,
                "needs_plan": False,
                "slots": {},
                "action": "answer",
                "direct_text": "你好，我是 Lvco，可以帮你分析数据。",
                "reason": "闲聊",
                "complexity": "simple",
            }, ensure_ascii=False)
        # 第二轮：上一轮为 answer → 必须 stop 收敛
        return json.dumps({
            "action": "stop",
            "tool_name": None,
            "tool_args": {},
            "direct_text": None,
            "reason": "上一轮已回答，无新任务",
            "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield ""  # direct_text 已提供，不会走到这里


async def test_first_round_answer_halts_loop_code_level():
    """纯回答路径：首轮 answer 后代码层直接收尾（不再依赖 LLM 第二轮自觉 stop）。

    曾测过"answer → 第二轮 stop"（方案 B：prompt 硬规则收敛），但真实环境 LLM
    对同类问题会连续多轮重复输出 answer——收敛必须落在代码层，r0 answer 即 break。
    """
    llm = _MergedAnswerThenStopLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1")

    events = [ev async for ev in agent.stream("你好，介绍一下你自己", ctx=ctx, db_session=None)]
    decisions = [e for e in events if e["type"] == "decision"]
    done = events[-1]

    assert llm.complete_calls == 1      # 合并一次即收尾，无第二轮决策
    assert len(decisions) == 1          # 只应有 r0
    assert decisions[0]["round"] == 0
    assert decisions[0]["action"] == "answer"
    assert done["type"] == "done"
    assert "lead_max_rounds_reached" not in done["degradations"]


class _AnalysisThenStopLLM:
    """合并调用返回 call_analysis（complexity=complex），第二轮返回 stop。

    验证多轮循环：子任务执行完毕后继续下一轮决策，第二轮依据子任务摘要收敛 stop，
    且 run_analysis 只执行一次（不做重复派发）。
    """

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        if self.complete_calls == 1:
            return json.dumps({
                "intent": "analysis",
                "confidence": 0.95,
                "needs_plan": True,
                "slots": {},
                "action": "call_analysis",
                "tool_name": "run_analysis",
                "tool_args": {"goal": "分析销售数据"},
                "direct_text": None,
                "reason": "多步分析任务",
                "complexity": "complex",
            }, ensure_ascii=False)
        return json.dumps({
            "action": "stop",
            "tool_name": None,
            "tool_args": {},
            "direct_text": None,
            "reason": "子任务已完成，主目标达成",
            "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield ""


async def test_call_analysis_then_round2_stop_run_once(fake_run_analysis):
    """多轮循环修复：r0 派发分析 → r1 依据子任务摘要 stop → run_analysis 只调用 1 次。"""
    llm = _AnalysisThenStopLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")

    events = [ev async for ev in agent.stream("帮我分析销售数据", ctx=ctx, db_session=None)]
    decisions = [e for e in events if e["type"] == "decision"]

    assert fake_run_analysis["n"] == 1            # 子任务只执行一次，无重复派发
    assert llm.complete_calls == 2                # 合并 + 第二轮决策
    assert [d["action"] for d in decisions] == ["call_analysis", "stop"]
    reports = [e for e in events if e["type"] == "report"]
    assert reports, "应有分析报告输出"


class _AlwaysAnswerLLM:
    """LLM 决策器永远输出 answer（模拟"列指标"这类简单问答，LLM 不主动 stop 的恶劣情况）。"""

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        return json.dumps({
            "intent": "data_qa",
            "confidence": 0.9,
            "needs_plan": False,
            "slots": {},
            "action": "answer",
            "direct_text": "当前数据源可用指标：销售额、订单量、客户数、客单价。",
            "reason": "简单事实问答，直接回答",
            "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield "当前数据源可用指标：销售额、订单量、客户数、客单价。"


async def test_always_answer_halts_loop_no_repeat():
    """防重复空转：LLM 连续输出 answer 时，代码层 r0 后必须收尾（用户实测 4 轮重复的回答 bug）。"""
    llm = _AlwaysAnswerLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")

    events = [ev async for ev in agent.stream("我现在有什么指标", ctx=ctx, db_session=None)]
    decisions = [e for e in events if e["type"] == "decision"]
    done = events[-1]

    assert llm.complete_calls == 1              # 无论 LLM 多爱回答，只有合并 1 次决策
    assert len(decisions) == 1                  # 只有 r0 一条 decision（无 r1/r2/r3 重复 answer）
    assert decisions[0]["action"] == "answer"
    # 输出正文整合后只有一份回答，不是 4 份重复
    body = "".join(ev.get("delta", "") for ev in events if ev.get("type") == "text")
    assert done["type"] == "done"
    assert "lead_max_rounds_reached" not in done["degradations"]


class _AnswerLlmWithMetricsCtx:
    """answer 分支（无 direct_text）需走 stream_chat 生成；记录 messages 供断言指标清单是否注入。"""

    def __init__(self) -> None:
        self.seen_messages: list[dict] = []

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        return json.dumps({
            "intent": "data_qa",
            "confidence": 0.9,
            "needs_plan": False,
            "slots": {},
            "action": "answer",
            "direct_text": None,          # 无直接文本 → 走 stream_chat 生成
            "reason": "简单问答",
            "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        self.seen_messages = messages
        yield "当前指标：销售额（sales_amount）"


async def test_answer_branch_injects_metrics_ctx():
    """answer 分支必须注入受治理指标清单，避免把裸字段（total_amount）当指标名回答。"""
    llm = _AnswerLlmWithMetricsCtx()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(
        user_id="u1", session_id="s1", entry="chat",
        metrics_ctx="sales_amount｜销售额｜SUM({{amount}})\norder_count｜订单量｜COUNT({{order_id}})",
    )

    events = [ev async for ev in agent.stream("我现在有什么指标", ctx=ctx, db_session=None)]
    joined = "\n".join(str(m.get("content", "")) for m in llm.seen_messages)
    texts = [ev.get("content", "") for ev in events if ev.get("type") == "text"]

    assert any("sales_amount" in joined for m in llm.seen_messages)  # 指标清单进上下文
    assert "受治理指标清单" in joined
    assert any("sales_amount" in t for t in texts)


async def test_resolve_subtask_mode_canvas_uses_complexity():
    """画布简单/复杂分流：canvas+simple → react；canvas+complex → canvas。"""
    from app.services.agents.lead.lead_decider import ActionType, Decision

    agent = LeadAgent(llm=None)

    simple = Decision(action=ActionType.CALL_ANALYSIS, complexity="simple")
    assert await agent._resolve_subtask_mode("canvas", simple, {}) == "react"

    complex_ = Decision(action=ActionType.CALL_ANALYSIS, complexity="complex")
    assert await agent._resolve_subtask_mode("canvas", complex_, {}) == "canvas"

    # 缺省 complexity → 按 complex 处理（画布保守走 CanvasOrchestrator）
    default = Decision(action=ActionType.CALL_ANALYSIS)
    assert await agent._resolve_subtask_mode("canvas", default, {}) == "canvas"


class _AskUserLLM:
    """决策输出 ask_user（反问），验证反问后仍必须发 done（前端才能停止 loading）。"""

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        return json.dumps({
            "intent": "canvas_edit",
            "confidence": 0.9,
            "needs_plan": False,
            "slots": {},
            "action": "ask_user",
            "direct_text": "请告诉我要修改哪个块。",
            "reason": "缺少目标块信息，需要澄清",
            "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield "请告诉我要修改哪个块。"


async def test_ask_user_still_emits_done():
    """ASK_USER 后必须走统一收尾并发出 done（曾直接 return 导致前端永远"执行中"）。"""
    llm = _AskUserLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="canvas")

    events = [ev async for ev in agent.stream("把标题改一下", ctx=ctx, db_session=None)]
    types = [e["type"] for e in events]

    assert "decision" in types
    decision = next(e for e in events if e["type"] == "decision")
    assert decision["action"] == "ask_user"
    assert types[-1] == "done", "ask_user 后必须 emit done 事件，否则前端流式状态卡死"
    text = "".join(ev.get("content", "") for ev in events if ev.get("type") == "text")
    assert "请告诉我要修改哪个块" in text


class _ContradictLLM:
    """模拟踩坑：LLM 输出 action=call_analysis 但 reason 自述"需要澄清"（你日志里的真实反例）。"""

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        return json.dumps({
            "intent": "canvas_edit",
            "confidence": 0.95,
            "needs_plan": False,
            "slots": {},
            "action": "call_analysis",
            "tool_name": "run_analysis",
            "tool_args": {"goal": "修改画布标题"},
            "direct_text": None,
            "reason": "用户明确要求修改画布标题，但缺少具体目标块信息，需要先询问澄清",
            "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield ""


async def test_contradict_call_analysis_corrected_to_ask_user(fake_run_analysis):
    """矛盾兜底：reason 说"需要澄清"却输出 call_analysis → 代码层纠正为 ask_user，不跑分析。"""
    llm = _ContradictLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="canvas")

    events = [ev async for ev in agent.stream("把标题改一下", ctx=ctx, db_session=None)]
    decisions = [e for e in events if e["type"] == "decision"]

    assert fake_run_analysis["n"] == 0, "矛盾决策被纠正，不应执行 run_analysis"
    assert decisions and decisions[0]["action"] == "ask_user"
    done = events[-1]
    assert done["type"] == "done"
    assert "lead_contradiction_fixed" in done.get("degradations", [])


async def test_final_summary_emitted_for_multi_subtask():
    """收尾总结：≥2 次子任务完成 → 循环结束后发一句汇总；单任务不触发。"""
    from app.services.agents.lead.lead_agent import LeadAgent as _LA

    agent = _LA(llm=None)
    ctx = LeadContext(user_id="u1", session_id="s1")

    # 单子任务：不触发
    ctx.turn_summaries.append("[子任务完成] 分析完成，结果已生成，请查看上方内容。")
    single = [ev for ev in [e async for e in agent._maybe_final_summary(ctx)]]
    assert not single

    # 双子任务：触发汇总
    ctx.turn_summaries.append("[子任务完成] 分析完成，已在画布上添加了 2 个内容块。")
    evs = [e async for e in agent._maybe_final_summary(ctx)]
    texts = [e["content"] for e in evs if e["type"] == "text"]
    assert texts and "完成 2 项分析" in texts[-1]
    assert "画布新增 1 个内容块" in texts[-1]
