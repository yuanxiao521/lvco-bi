# -*- coding: utf-8 -*-
"""P2 评审阶段：HARD 硬闸、SOFT 条件裁量、安全边界与降级。

任务书 §4 P2 明确要求"安全边界（必须写测试）"：
  · hard 非空 → verdict 强制 fail（Lead 不得翻案）
且验收要求"多数任务零新增调用（无 SOFT 不触发）"。
"""
from __future__ import annotations

from app.services.agents.lead.lead_review_stage import (
    _extract_json,
    build_adjudicate_messages,
    run_review_stage,
)
from app.services.agents.lead.lead_tools import RunAnalysisResult


class FakeLLM:
    """只实现裁量需要的 complete()；记录调用次数（验证"该不该花 LLM"）。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def complete(self, messages, **kw):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _ok(**kw) -> RunAnalysisResult:
    base = dict(success=True, report="r", steps=[], blocks_added=2,
                error=None, verified=True, tool_chain=[{"name": "run_query", "ok": True}])
    base.update(kw)
    return RunAnalysisResult(**base)


# ── HARD 硬闸（安全边界，必须写测试）───────────────────────────────────────

async def test_hard_failure_forces_fail_without_llm():
    """hard 非空 → 强制 fail，且**一次 LLM 都不花**（HARD 是代码事实，不需要裁量）。"""
    llm = FakeLLM('{"verdict": "pass", "reason": "我觉得还行"}')
    outcome = await run_review_stage(
        _ok(success=False, error="查询超时"), entry="chat", user_goal="做个分析", llm=llm,
    )
    assert outcome.verdict == "fail", "HARD 非空时不得被翻案"
    assert outcome.hard and not outcome.adjudicated
    assert llm.calls == 0, "HARD 判定不该花 LLM"


async def test_canvas_zero_blocks_is_hard_and_not_adjudicated():
    """画布入口成功却零落块 = HARD（该干活的没落到画布上）。"""
    llm = FakeLLM('{"verdict": "pass"}')
    outcome = await run_review_stage(
        _ok(blocks_added=0, tool_chain=[]), entry="canvas", user_goal="画个图", llm=llm,
    )
    assert outcome.verdict == "fail"
    assert any("未在画布上产出内容块" in h for h in outcome.hard)
    assert llm.calls == 0


# ── 无 SOFT：多数任务的路径，零新增调用 ────────────────────────────────────

async def test_no_soft_means_pass_without_llm():
    """§4 P2 验收：多数任务零新增调用（无 SOFT 不触发裁量）。"""
    llm = FakeLLM("不该被调用")
    outcome = await run_review_stage(_ok(), entry="chat", user_goal="查一下销售额", llm=llm)
    assert outcome.verdict == "pass" and outcome.passed
    assert outcome.adjudicated is False
    assert llm.calls == 0


async def test_canvas_exemption_keeps_verified_soft_empty():
    """画布豁免照旧：画布已产出内容块时"数字未过后验"不进 SOFT → 零调用。"""
    llm = FakeLLM("不该被调用")
    outcome = await run_review_stage(
        _ok(verified=False, blocks_added=3), entry="canvas", user_goal="画个图", llm=llm,
    )
    assert outcome.hard == [] and outcome.soft == []
    assert outcome.verdict == "pass"
    assert llm.calls == 0


# ── 仅 SOFT：一次裁量，由代码触发 ──────────────────────────────────────────

async def test_soft_only_triggers_exactly_one_adjudication():
    llm = FakeLLM('{"verdict": "pass", "reason": "数字存疑但不影响结论", "guidance": ""}')
    outcome = await run_review_stage(
        _ok(verified=False), entry="chat", user_goal="查一下销售额", llm=llm,
    )
    assert outcome.adjudicated is True
    assert llm.calls == 1, "仅 SOFT 时应恰好一次裁量"
    assert outcome.verdict == "pass"
    assert outcome.soft and not outcome.hard


async def test_adjudication_fail_carries_guidance():
    llm = FakeLLM('{"verdict": "fail", "reason": "缺品牌维度", "guidance": "补一个按品牌的图"}')
    outcome = await run_review_stage(
        _ok(verified=False), entry="chat", user_goal="按品类和品牌分析", llm=llm,
    )
    assert outcome.verdict == "fail"
    assert "补一个按品牌的图" in outcome.guidance


# ── 降级（§6 不变量 4）────────────────────────────────────────────────────

async def test_adjudication_exception_degrades_to_code_conclusion():
    """裁量失败 → 退回纯代码结论（有 SOFT 无 HARD → 保守判 fail）并记降级。"""
    llm = FakeLLM(RuntimeError("upstream 502"))
    degradation: list[str] = []
    outcome = await run_review_stage(
        _ok(verified=False), entry="chat", user_goal="查一下", llm=llm, degradation=degradation,
    )
    assert outcome.verdict == "fail"
    assert "lead_adjudicate_failed" in degradation


async def test_adjudication_invalid_json_degrades():
    llm = FakeLLM("我觉得可以，随便吧")
    degradation: list[str] = []
    outcome = await run_review_stage(
        _ok(verified=False), entry="chat", user_goal="查一下", llm=llm, degradation=degradation,
    )
    assert outcome.verdict == "fail"
    assert "lead_adjudicate_failed" in degradation


# ── 解析与输入组装 ─────────────────────────────────────────────────────────

def test_extract_json_tolerates_code_fences_and_noise():
    fence = chr(96) * 3
    raw = fence + 'json\n{"verdict": "pass", "reason": "ok"}\n' + fence
    assert _extract_json(raw)["verdict"] == "pass"
    assert _extract_json('前缀 {"verdict": "fail"} 后缀')["verdict"] == "fail"
    assert _extract_json("没有 JSON") == {}


def test_build_adjudicate_messages_includes_goal_soft_and_summary():
    msgs = build_adjudicate_messages(
        user_goal="按品类和品牌分析", soft=["维度覆盖不全"],
        execution_summary="[子任务执行记录] ...",
    )
    assert msgs[0]["role"] == "system"
    body = msgs[1]["content"]
    for token in ("按品类和品牌分析", "维度覆盖不全", "子任务执行记录"):
        assert token in body, f"裁量输入缺少 {token}"
