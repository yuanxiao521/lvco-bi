# -*- coding: utf-8 -*-
"""P3：观察工具（recon_tool / list_metrics）+ Lead 声明的验收标准（acceptance）。

任务书 §4 P3：
  · 注册只读工具，每轮 ≤2 次，独立 trace 名 lead_recon；
  · 失败即降级（拿不到就按现有路径走）；
  · Decision 增 acceptance（结构化、可核验）；
  · assess_subtask 增第 6 项「Lead 声明的标准未满足」（**声明缺失时跳过**，向后兼容）；
  · acceptance **不进幂等键**（否则改声明就击穿 memo）。
"""
from __future__ import annotations

from app.services.agents.lead.lead_observe import list_metrics, recon_tool
from app.services.agents.lead.lead_review_stage import run_review_stage
from app.services.agents.lead.lead_tools import (
    RunAnalysisArgs,
    RunAnalysisResult,
    assess_subtask_detail,
    effective_memo_key,
    make_idempotency_key,
)


def _ok(**kw) -> RunAnalysisResult:
    base = dict(success=True, report="r", steps=[], blocks_added=2,
                error=None, verified=True, tool_chain=[{"name": "run_query", "ok": True}])
    base.update(kw)
    return RunAnalysisResult(**base)


class _Ctx:
    entry = "chat"
    session_id = "s1"
    user_id = "u1"
    datasource_id = None
    canvas_state = None

    def __init__(self) -> None:
        self.history_summary = ""
        self.metrics_ctx = "【销售额】= SUM(amount) 单位：元"
        self.turn_summaries: list[str] = []


class _LLM:
    def __init__(self, reply='{"verdict": "pass"}'):
        self.reply = reply
        self.calls = 0

    async def complete(self, messages, **kw):
        self.calls += 1
        return self.reply


# ── acceptance：第 6 项核验 ────────────────────────────────────────────────

def test_acceptance_absent_keeps_legacy_verdict():
    """**向后兼容**：不声明 acceptance 时，判定与以前完全一致。"""
    d = assess_subtask_detail(_ok(), entry="chat", user_goal="查一下")
    assert d.hard == [] and d.soft == []
    assert d.passed is True


def test_acceptance_min_blocks_unmet_goes_soft():
    d = assess_subtask_detail(
        _ok(blocks_added=1), entry="canvas", user_goal="画图",
        acceptance={"min_blocks": 3},
    )
    assert any("Lead 声明的标准未满足" in s for s in d.soft), "第 6 项没生效"
    assert d.hard == [], "声明未满足属 SOFT（需判断），不是 ①②③ 那种事实性失败"
    assert d.passed is False


def test_acceptance_min_blocks_met_is_silent():
    d = assess_subtask_detail(
        _ok(blocks_added=3), entry="canvas", user_goal="画图",
        acceptance={"min_blocks": 3},
    )
    assert not any("Lead 声明的标准" in s for s in d.soft + d.hard)


def test_acceptance_require_verified():
    d = assess_subtask_detail(_ok(verified=False), entry="chat", user_goal="查一下",
                              acceptance={"require_verified": True})
    assert any("要求报告数字经过后验" in s for s in d.soft)


def test_unknown_acceptance_keys_are_ignored():
    """认不出来的键一律忽略——不猜、不臆断用户意图。"""
    d = assess_subtask_detail(_ok(), entry="chat", user_goal="查一下",
                              acceptance={"must_be_beautiful": True})
    assert d.hard == [] and d.soft == []


def test_acceptance_does_not_enter_memo_key():
    """§4 P3：acceptance **不进幂等键**——改声明不该击穿 memo（与 guidance 相反）。"""
    args = RunAnalysisArgs(goal="G", datasource_id=1, canvas_id=None, entry="chat")
    # effective_memo_key 只吃 args + guidance，签名里根本没有 acceptance
    assert effective_memo_key(args, "") == make_idempotency_key(args)
    assert effective_memo_key(args, "补维度") != make_idempotency_key(args)


async def test_review_stage_verifies_acceptance():
    """评审阶段要把 Lead 声明的标准算进去（声明未满足 → 至少 SOFT 非空）。"""
    llm = _LLM('{"verdict": "fail", "reason": "块数不够", "guidance": "再补一块"}')
    outcome = await run_review_stage(
        _ok(blocks_added=1), entry="canvas", user_goal="画图", llm=llm,
        acceptance={"min_blocks": 2},
    )
    assert any("Lead 声明的标准未满足" in s for s in outcome.soft)
    assert outcome.verdict == "fail"


# ── 观察工具 ───────────────────────────────────────────────────────────────

def test_list_metrics_reads_injected_metrics_context():
    """同一事实只有一个来源：直接读已注入上下文的受治理指标清单。"""
    assert list_metrics(_Ctx()) == "【销售额】= SUM(amount) 单位：元"
    assert list_metrics(object()) == ""


async def test_recon_tool_query_sample_degrades_without_db_session():
    """取不到时返回 None 并记降级——**绝不假装成功**。

    历史：query_sample 曾经是未接线的桩（记 unimplemented 后返回 None）；
    P3 补线后改走真实取数，失败口径细化成具体原因（此处：拿不到数据源清单）。
    降级原因的完整矩阵见 tests/test_lead_p3_query_sample.py。
    """
    degradation: list[str] = []
    got = await recon_tool("query_sample", ctx=_Ctx(), args={}, db_session=None,
                           degradation=degradation)
    assert got is None
    assert any("query_sample" in d for d in degradation)


async def test_recon_tool_unknown_name_degrades():
    degradation: list[str] = []
    got = await recon_tool("no_such_tool", ctx=_Ctx(), args={}, db_session=None,
                           degradation=degradation)
    assert got is None
    assert any("observe_unimplemented" in d for d in degradation)


async def test_recon_tool_failure_degrades_and_returns_none():
    """观察失败 → None + 降级（§6 不变量 4：拿不到就按现有路径走）。"""
    class Boom:  # canvas_state.layout 抛错 → get_canvas_layout 内部已吞，返回 ""
        async def layout(self, force: bool = False):
            raise RuntimeError("db down")

    ctx = _Ctx()
    ctx.canvas_state = Boom()
    degradation: list[str] = []
    got = await recon_tool("get_canvas_layout", ctx=ctx, args={}, db_session=None,
                           degradation=degradation)
    assert got == ""            # 内部降级为空串，不抛
    assert degradation == []


async def test_recon_tool_list_metrics_works_without_db():
    got = await recon_tool("list_metrics", ctx=_Ctx(), args={}, db_session=None)
    assert "销售额" in got

# ── 方案 4：清空/删块类任务的零落块豁免 ─────────────────────────────────────

def _result(**kw):
    base = dict(success=True, report="r", steps=[], blocks_added=0,
                error=None, verified=True, tool_chain=[])
    base.update(kw)
    from app.services.agents.lead.lead_tools import RunAnalysisResult
    return RunAnalysisResult(**base)


def _assess(result, goal=""):
    from app.services.agents.lead.lead_tools import assess_subtask_detail
    return assess_subtask_detail(result, entry="canvas", user_goal=goal)


def test_clear_canvas_task_zero_blocks_passes():
    """清空画布：成功 + 零落块 + tool_chain 有 clear_canvas 成功记录 → 不判 HARD。"""
    out = _assess(_result(tool_chain=[{"tool": "clear_canvas", "ok": True, "cleared": 16}]),
                  goal="把画布清空")
    assert out.passed, out.hard
    assert not any("零落块" in h or "内容块" in h for h in out.hard)


def test_remove_block_task_zero_blocks_passes():
    """删块任务同理（编排器路径 tool_chain 用 name 键）。"""
    out = _assess(_result(tool_chain=[{"name": "remove_block", "ok": True}]),
                  goal="删除那张趋势图")
    assert out.passed, out.hard


def test_do_nothing_task_zero_blocks_still_fails():
    """真·什么都没干（成功但无任何变更操作）→ 仍然 HARD 未达标（豁免不放过懒任务）。"""
    out = _assess(_result(tool_chain=[]), goal="分析一下销售趋势")
    assert any("未在画布上产出内容块" in h for h in out.hard), out.hard


def test_add_chart_zero_blocks_via_failed_tools_still_hard():
    """变更工具失败（ok=False）不算"变更成功"——零落块仍判 HARD（豁免只认成功变更）。"""
    out = _assess(_result(tool_chain=[{"name": "add_chart_block", "ok": False}]),
                  goal="画一张柱状图")
    assert any("未在画布上产出内容块" in h for h in out.hard), out.hard
