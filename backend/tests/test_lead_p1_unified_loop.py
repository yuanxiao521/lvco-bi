# -*- coding: utf-8 -*-
"""P1 统一循环：收敛闸、配额、拒绝未知工具、确定性降级。

任务书 §4 P1 的验收之一是「收敛闸测试绿」。这些用例用**假 LLM** 驱动循环，
把闸门行为钉死——真机评测只能证明"平均效果好"，证明不了"绝不会失控"。
"""
from __future__ import annotations

import pytest

from app.services.agents.lead import lead_agent_unified as U


class FakeCtx:
    entry = "chat"
    session_id = "s1"
    user_id = "u1"
    datasource_id = None
    canvas_state = None

    def __init__(self) -> None:
        self.history_summary = ""
        self.metrics_ctx = ""
        self.turn_summaries: list[str] = []
        self.turns: list[dict] = []

    def recent_turns(self, max_turns: int = 8, max_chars=None):
        return list(self.turns)

    def add_turn(self, role: str, content: str) -> None:
        self.turns.append({"role": role, "content": content})


class FakeLLM:
    """按脚本逐轮吐事件；记录调用次数（用于验证"闲聊 1 次调用"）。"""

    def __init__(self, rounds: list[list[dict]]) -> None:
        self.rounds = list(rounds)
        self.calls = 0

    async def tool_call_round(self, messages, tools, **kw):
        self.calls += 1
        events = self.rounds.pop(0) if self.rounds else []
        for ev in events:
            yield ev


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": args}


async def _run(rounds, *, max_steps=6, **kw):
    llm = FakeLLM(rounds)
    ctx = FakeCtx()
    events = []
    async for ev in U.stream_unified(
        user_msg="你好", ctx=ctx, db_session=None, llm=llm,
        available_datasources=[], max_steps=max_steps, **kw,
    ):
        events.append(ev)
    return events, llm, ctx


# ── 终止动作 ───────────────────────────────────────────────────────────────

async def test_chitchat_finishes_in_one_llm_call():
    """闲聊：一轮 tool_call_round 就收尾（P1 验收第 1 条）。"""
    events, llm, _ = await _run([[_tc("answer", {"text": "你好，有什么可以帮你？"})]])

    assert llm.calls == 1
    assert any(e.get("type") == "report" and "你好" in e.get("content", "") for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["degraded"] is False


async def test_stop_finishes_immediately():
    events, llm, _ = await _run([[_tc("stop", {"reason": "已完成"})]])
    assert llm.calls == 1
    assert events[-1]["type"] == "done"


async def test_ask_user_confirm_emits_confirm_request(monkeypatch):
    monkeypatch.setattr(
        "app.services.lead_session_state.set_pending", lambda *a, **k: None, raising=False)
    events, _, _ = await _run([[_tc("ask_user", {"question": "要清空画布吗？", "ask_kind": "confirm"})]])
    assert any(e.get("type") == "confirm_request" for e in events), "confirm 卡片链路断了"
    assert events[-1]["type"] == "done"


# ── 汇报层：decision 事件（method a：reason 由模型自述）──────────────────

async def test_terminal_tool_emits_decision_event_with_model_reason():
    """legacy 每轮都 emit decision(action/tool/reason)，前端 agentMeta 据此显示
    "为什么这么判断"。统一循环把工具调用当决策点，reason 取模型自述（不是回填话术）。"""
    events, _, _ = await _run([[_tc("answer", {"text": "好的", "reason": "闲聊直接回答"})]])

    dec = [e for e in events if e.get("type") == "decision"]
    assert len(dec) == 1
    assert dec[0]["action"] == "answer"
    assert dec[0]["tool"] == "answer"
    assert dec[0]["reason"] == "闲聊直接回答"
    # 顺序与 legacy 一致：先 decision 再正文
    report_idx = next(i for i, e in enumerate(events) if e.get("type") == "report")
    assert events.index(dec[0]) < report_idx


async def test_observe_tools_do_not_emit_decision():
    """观察只是"看一眼"，不发 decision（legacy 也没有对应动作）；
    前端用 tool_call/tool_result 事件已经能看到。"""
    events, _, _ = await _run([[_tc("get_canvas_layout", {}, "c1")], [_tc("stop", {})]])
    assert not any(
        e.get("type") == "decision" and e.get("tool") == "get_canvas_layout"
        for e in events
    ), "观察类不该发 decision"


# ── 循环闸（§2.3）──────────────────────────────────────────────────────────

async def test_step_limit_forces_stop():
    """步数闸：模型一直调工具不收尾 → 到上限必须由**代码**强制停止。"""
    rounds = [[_tc("get_canvas_layout", {}, f"c{i}")] for i in range(10)]
    events, llm, _ = await _run(rounds, max_steps=3)

    assert llm.calls == 3, "步数上限没有生效"
    assert any(e.get("type") == "status" for e in events)
    done = events[-1]
    assert done["type"] == "done"
    assert "lead_agent_step_limit" in done["degradations"]


async def test_observe_quota_is_enforced():
    """观察类配额 ≤2：第 3 次起被拒绝，且记降级。"""
    rounds = [[_tc("get_canvas_layout", {}, f"c{i}")] for i in range(5)]
    events, _, _ = await _run(rounds, max_steps=5)

    results = [e for e in events if e.get("type") == "tool_result"]
    assert len(results) == 2, f"观察类配额应为 2，实际执行了 {len(results)} 次"
    done = events[-1]
    assert "lead_agent_quota_observe" in done["degradations"]


# ── 硬边界与降级 ───────────────────────────────────────────────────────────

async def test_write_tool_call_is_rejected():
    """写工具不在工具表里 → 必须拒绝并记降级，绝不"尽力执行"。"""
    events, _, _ = await _run([[_tc("add_chart_block", {"chart_type": "bar"})]])

    assert any(e.get("type") == "tool_result" for e in events) is False, "写工具被当成工具执行了"
    done = events[-1]
    assert "lead_agent_unknown_tool" in done["degradations"]


async def test_text_without_tool_call_becomes_answer():
    """模型只给文本、不调工具 → 当作回答（确定性降级），不是死循环。"""
    events, llm, _ = await _run([[{"type": "text", "content": "结论：East 居首。"}]])
    assert llm.calls == 1
    assert any(e.get("type") == "report" and "East" in e.get("content", "") for e in events)
    assert events[-1]["type"] == "done"


async def test_llm_failure_degrades_deterministically():
    """LLM 抛错 → 确定性收尾（§6 不变量 4），不空转。"""
    class BoomLLM:
        calls = 0

        async def tool_call_round(self, messages, tools, **kw):
            BoomLLM.calls += 1
            raise RuntimeError("upstream 502")
            yield  # pragma: no cover

    events = []
    async for ev in U.stream_unified(
        user_msg="你好", ctx=FakeCtx(), db_session=None, llm=BoomLLM(),
        available_datasources=[], max_steps=3,
    ):
        events.append(ev)

    assert BoomLLM.calls == 1, "LLM 失败后不得重试空转"
    assert any(e.get("type") == "error" for e in events)
    assert "lead_agent_llm_failed" in events[-1]["degradations"]


# ── complexity 确定性兜底 ──────────────────────────────────────────────────

def test_effective_complexity_missing_or_invalid_defaults_to_complex():
    """缺失/非法 → complex（与 legacy 的确定性兜底一致），且不算"推翻模型"。"""
    for bad in ("", "huge", None):
        assert U.effective_complexity("随便看看", str(bad or "")) == ("complex", False)


def test_effective_complexity_keeps_model_choice_when_complex():
    assert U.effective_complexity("做个报告", "complex") == ("complex", False)


def test_effective_complexity_upgrades_simple_for_multi_step_goals():
    """实测根因兜底：模型把明显多步的目标判成 simple → 抬回 complex 并记 override。

    依据：双跑对拍显示 unified 63% 派发走 react（legacy 37%），react 工具轮更贵，
    20 题多出 47 次 LLM 调用。这条兜底就是用来压这个偏差的。
    """
    for goal in ("生成柱状图和饼图并写详细分析", "按地区对比销售额趋势", "搭一个销售看板"):
        complexity, overridden = U.effective_complexity(goal, "simple")
        assert complexity == "complex", f"{goal!r} 应抬回 complex"
        assert overridden is True, f"{goal!r} 的抬回应记为 override（便于观测）"


def test_effective_complexity_allows_simple_for_single_lookup():
    """真正的单值查询不该被抬走——否则兜底会变成"永远走重路径"。"""
    assert U.effective_complexity("华东区销售额是多少", "simple") == ("simple", False)
    assert U.effective_complexity("有几个数据源", "simple") == ("simple", False)


# ── 模式开关与包装层顺序 ───────────────────────────────────────────────────

def test_lead_agent_mode_defaults_to_legacy_and_rejects_garbage(monkeypatch):
    """非法配置值必须退回 legacy——配置写错时回到已验证路径，而不是进新路。

    取值就叫 single_agent（单 Agent），不叫 unified：unified 与 P4「装配统一」同词
    不同事，读代码的人无法判断指哪一层。旧值保留为别名，避免环境里写着旧值时静默退回 legacy。
    """
    from app.config import settings
    from app.services.agents.lead.lead_agent import lead_agent_mode

    monkeypatch.setattr(settings, "LEAD_AGENT_MODE", "legacy")
    assert lead_agent_mode() == "legacy"
    monkeypatch.setattr(settings, "LEAD_AGENT_MODE", "Single_Agent")
    assert lead_agent_mode() == "single_agent"
    monkeypatch.setattr(settings, "LEAD_AGENT_MODE", "  single_agent  ")
    assert lead_agent_mode() == "single_agent"
    # 旧值兼容：改名不能让人"配置写了但静默走了 legacy"
    monkeypatch.setattr(settings, "LEAD_AGENT_MODE", "unified")
    assert lead_agent_mode() == "single_agent"
    for bad in ("", None, "v2", "experimental"):
        monkeypatch.setattr(settings, "LEAD_AGENT_MODE", bad)
        assert lead_agent_mode() == "legacy", f"{bad!r} 应退回 legacy"


async def test_stream_unified_wrapper_emits_done_last(monkeypatch):
    """后处理（收尾总结 / 记忆回流）必须排在 done **之前**。

    顺序反了的话，前端收到 done 就解锁输入并清 streaming 态，
    而记忆事件还在后面——观感与状态都会错乱。
    """
    from app.services.agents.lead import lead_agent as LA
    from app.services.agents.lead import lead_agent_unified as U

    seen: dict = {}

    async def fake_stream_unified(**kw):
        seen["emit_done"] = kw.get("emit_done")
        yield {"type": "report", "content": "正文", "source": "lead"}

    async def fake_summarize(self, ctx, db_session=None, trace=None):
        return {"summary": "记忆摘要", "covered_rounds": 1}

    monkeypatch.setattr(U, "stream_unified", fake_stream_unified)
    monkeypatch.setattr(LA.LeadAgent, "_maybe_summarize", fake_summarize)

    agent = LA.LeadAgent(llm=object())
    events = [
        ev async for ev in agent._stream_unified(
            "你好", ctx=LA.LeadContext(user_id="u1"), db_session=None
        )
    ]
    types = [e["type"] for e in events]

    assert seen["emit_done"] is False, "包装层必须自己发 done（否则后处理插不进去）"
    assert types[-1] == "done", f"done 必须在最后，实际顺序 {types}"
    assert "compressed_history" in types
    assert types.index("compressed_history") < types.index("done")


# ── 上下文装配 ─────────────────────────────────────────────────────────────

def test_context_assembly_moved_to_provider_outlet():
    """P4：上下文装配**已收归** TurnContextProvider.for_agent_loop。

    本模块不再有 build_lead_messages（临时实现已删）——若它被重新加回来，
    就等于又开了一条装配路径，统一装配白做。
    """
    assert not hasattr(U, "build_lead_messages"), "临时装配函数不该回来"
    assert not hasattr(U, "_render_datasources"), "渲染应收敛到 context/sources"
