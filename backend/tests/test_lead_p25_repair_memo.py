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
