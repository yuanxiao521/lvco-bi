# -*- coding: utf-8 -*-
"""P2.5.1：redo / repair 不得被 memo 吞掉。

事实（任务书已核实）：make_idempotency_key 刻意不含 guidance，于是**同一请求内**
用同一个 goal 重派会命中上一轮的 memo → 直接返回"未达标"的旧结果 → redo 完全无效；
日志里只显示 memo_hit，**看起来像"执行过了"**。

修法（A + B 都做）：
  A. 显式重做（同 goal 再派）跳过 memo——"重做"不是"重复执行"；
  B. 把 guidance 纳入幂等键（**仅非空时**，无 guidance 的路径键不变 → legacy 零变化）。
"""
from __future__ import annotations

from app.services.agents.lead.lead_tools import (
    RunAnalysisArgs,
    effective_memo_key,
    make_idempotency_key,
)


# ── 修法 B：幂等键 ─────────────────────────────────────────────────────────

def test_memo_key_unchanged_without_guidance():
    """无 guidance 时键必须**一字不变**——否则 legacy 的 memo 行为被改动。"""
    for entry in ("chat", "canvas"):
        args = RunAnalysisArgs(goal="查销售额", datasource_id=1, canvas_id=None, entry=entry)
        assert effective_memo_key(args) == make_idempotency_key(args)
        assert effective_memo_key(args, "") == make_idempotency_key(args)


def test_memo_key_differs_per_guidance_and_is_stable():
    """不同 guidance → 不同键（改了指令就该重跑）；同一 guidance → 键稳定（缓存仍有效）。"""
    args = RunAnalysisArgs(goal="查销售额", datasource_id=1, canvas_id=None, entry="chat")
    base = make_idempotency_key(args)
    a = effective_memo_key(args, "补上品牌维度")
    b = effective_memo_key(args, "标题改成中文")

    assert a != base, "带 guidance 的键必须与原键不同，否则重派会被 memo 吞"
    assert b != base
    assert a != b, "不同 guidance 应视为不同的实质输入"
    assert a == effective_memo_key(args, "补上品牌维度"), "同一 guidance 的键必须稳定"


# ── P2.5：repair_kind（后续轮的路由意图）──────────────────────────────────

def _intent():
    from app.services.agents.lead.lead_intent import IntentResult, IntentType
    return IntentResult(intent=IntentType.ANALYSIS, confidence=0.9,
                        needs_plan=True, degraded=False)


def _parse(obj: dict, degradation=None):
    from app.services.agents.lead.lead_decider import _parse_decision
    return _parse_decision(obj, "做个分析", _intent(), degradation)


def test_repair_kind_parsed_from_llm_output():
    d = _parse({"action": "call_analysis", "tool_args": {"goal": "补品牌图"},
                "repair_kind": "patch"})
    assert d.repair_kind == "patch"
    assert d.degraded is False


def test_repair_kind_and_complexity_together_repair_kind_wins():
    """两者同时出现 = 模型输出不自洽 → **以 repair_kind 为准** + 记降级 + 告警。

    不让 LLM 同时给两个字段并要求自洽，等于主动给它制造犯错余地。
    """
    degradation: list[str] = []
    d = _parse({"action": "call_analysis", "tool_args": {"goal": "补品牌图"},
                "repair_kind": "redo", "complexity": "simple"}, degradation)
    assert d.repair_kind == "redo"
    assert d.degraded is True
    assert "lead_decision_inconsistent_repair_kind" in degradation


def test_invalid_repair_kind_is_dropped():
    assert _parse({"action": "call_analysis", "tool_args": {"goal": "G"},
                   "repair_kind": "重做"}).repair_kind == ""
    assert _parse({"action": "call_analysis", "tool_args": {"goal": "G"}}).repair_kind == ""


def test_repair_kind_maps_to_kernel():
    """§4 P2.5：repair_kind **内含路由意图**，不需要新表。"""
    from app.services.agents.lead.lead_agent_tools import REPAIR_KIND_KERNEL
    assert REPAIR_KIND_KERNEL == {"patch": "react", "redo": "orchestrator"}


def test_explicit_kernel_wins_in_resolve_worker():
    """repair_kind 给出的显式内核必须压过「入口 × 复杂度」表。"""
    from app.services.agents.lead.lead_worker import resolve_worker
    # chat+complex 本来是 orchestrator；显式给 react 时应为 react（patch 语义）
    assert resolve_worker("chat", "complex").mode == "orchestrator"
    assert resolve_worker("chat", "complex", "react").mode == "react"
    # chat+simple 本来是 react；显式给 orchestrator 时应为 orchestrator（redo 语义）
    assert resolve_worker("chat", "simple").mode == "react"
    assert resolve_worker("chat", "simple", "orchestrator").mode == "orchestrator"


def test_repair_has_independent_class_and_quota():
    from app.services.agents.lead.lead_agent_tools import QUOTA, tool_class
    assert tool_class("repair") == "repair", "repair 必须有独立类别，否则会占派发额度"
    assert tool_class("dispatch_analysis") == "dispatch"
    assert QUOTA["repair"] == 2


# ── 修法 A：循环里的显式跳过 ───────────────────────────────────────────────

class _Ctx:
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


class _LLM:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    async def tool_call_round(self, messages, tools, **kw):
        for ev in (self.rounds.pop(0) if self.rounds else []):
            yield ev


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"type": "tool_call", "id": call_id, "name": name, "arguments": args}


async def _drive(rounds, monkeypatch, max_steps: int = 5):
    """用假 run_worker 驱动循环，收集每次派发收到的 kwargs。"""
    from app.services.agents.lead.lead_agent_unified import stream_unified
    from app.services.agents.lead.lead_tools import RunAnalysisResult

    seen: list[dict] = []

    async def fake_run_worker(**kw):
        seen.append(kw)
        cb = kw.get("on_result")
        if cb:
            cb(RunAnalysisResult(success=True, blocks_added=1,
                                 tool_chain=[{"name": "run_query", "ok": True}]))
        return
        yield  # 让它成为 async generator

    monkeypatch.setattr(
        "app.services.agents.lead.lead_dispatch.run_worker", fake_run_worker)
    async for _ in stream_unified(
        user_msg="分析一下", ctx=_Ctx(), db_session=None, llm=_LLM(rounds),
        available_datasources=[], max_steps=max_steps,
    ):
        pass
    return seen


async def test_first_dispatch_does_not_bypass_memo(monkeypatch):
    """首次派发不该跳过 memo——否则幂等缓存形同虚设、子任务会被重复执行。"""
    seen = await _drive(
        [[_tc("dispatch_analysis", {"goal": "G"})], [_tc("answer", {"text": "ok"})]],
        monkeypatch,
    )
    assert len(seen) == 1
    assert not seen[0].get("bypass_memo")


async def test_same_goal_redispatched_bypasses_memo(monkeypatch):
    """**P2.5.1 核心**：同一请求内同 goal 再派 = 重做，必须跳过 memo。

    不跳过的话会命中上一轮的缓存 → 直接返回未达标的旧结果 → redo 完全无效。
    """
    seen = await _drive(
        [
            [_tc("dispatch_analysis", {"goal": "G"}, "c1")],
            [_tc("dispatch_analysis", {"goal": "G"}, "c2")],
            [_tc("answer", {"text": "ok"})],
        ],
        monkeypatch,
    )
    assert len(seen) == 2, "两次派发都应真的执行"
    assert not seen[0].get("bypass_memo"), "首次不跳过"
    assert seen[1].get("bypass_memo") is True, "同 goal 重派必须跳过 memo"


async def test_repair_always_bypasses_memo(monkeypatch):
    """repair 是"补缺口"，本质是重做，必须跳过 memo。"""
    seen = await _drive(
        [[_tc("repair", {"gap": "缺品牌维度", "goal": "补品牌图"})],
         [_tc("answer", {"text": "ok"})]],
        monkeypatch,
    )
    assert len(seen) == 1
    assert seen[0].get("bypass_memo") is True


# ── 内核选择：repair 走 legacy 同一张映射表 ────────────────────────────────

def test_repair_kernel_comes_from_legacy_mapping_table():
    """repair → 轻量内核（react）必须取自 REPAIR_KIND_KERNEL，不另立词汇。

    legacy 是把 `REPAIR_KIND_KERNEL[repair_kind]` 当 explicit 传给 resolve_worker
    （lead_agent.py:649-658）；统一循环没有 repair_kind 字段，但语义上 repair 就是
    patch。这里显式取表，避免靠 complexity="simple" 间接落到 react——
    否则哪天表改了，两处会静默漂移。
    """
    from app.services.agents.lead.lead_agent_tools import REPAIR_KIND_KERNEL
    from app.services.agents.lead.lead_agent_unified import explicit_kernel

    assert explicit_kernel("repair") == REPAIR_KIND_KERNEL["patch"] == "react"
    # 普通派发不给 explicit：仍按「入口 × 复杂度」表路由
    assert explicit_kernel("dispatch_analysis") is None
    assert explicit_kernel("") is None


async def test_different_goals_do_not_bypass(monkeypatch):
    """不同 goal 是两次独立子任务，各自走 memo 即可（不该无脑跳过）。"""
    seen = await _drive(
        [
            [_tc("dispatch_analysis", {"goal": "G1"}, "c1")],
            [_tc("dispatch_analysis", {"goal": "G2"}, "c2")],
            [_tc("answer", {"text": "ok"})],
        ],
        monkeypatch,
    )
    assert [bool(k.get("bypass_memo")) for k in seen] == [False, False]
