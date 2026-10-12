# -*- coding: utf-8 -*-
"""装配层等价性回归：Provider/Assembler 的输出必须与旧实现**逐字节相同**。

这是 P0（行为零变化）的安全网。旧实现指：
- LeadContext.digest()（决策窗口）
- LeadContext.recent_turns()（回答窗口）
- lead_decider._build_context_blocks()（状态四块）
- lead_agent._answer_branch() 的 messages 拼装顺序
"""
import json

import pytest

from app.services.context.assembler import (
    Assembler,
    render_window_for_decision,
)
from app.services.context.provider import TurnContextProvider, TurnInputs
from app.services.context.sources.state import (
    render_assessment,
    render_canvas,
    render_datasources,
    render_subtask_summaries,
)
from app.services.context.sources.window import select_for_answer
from app.services.context.types import Block, BlockKind
from app.services.context_utils import align_history_pairs


def _turns(n: int = 6) -> list[dict]:
    turns: list[dict] = []
    for i in range(1, n + 1):
        turns.append({"role": "user", "content": f"第{i}问：帮我看看华东销售额"})
        turns.append({"role": "assistant", "content": f"第{i}答：华东销售额 {i} 万（含税口径，2026Q3）"})
    return turns


# ── 决策窗口：render_window_for_decision == LeadContext.digest ──────────────

def test_decision_window_matches_digest_byte_for_byte():
    from app.services.agents.lead.lead_agent import LeadContext

    for extra in ("", "【上一轮已生成的图表】柱状图"):
        for summary in ("", "【口径与规则】销售额按全公司含税口径计算"):
            for turns in (_turns(3), _turns(30), []):
                ctx = LeadContext(user_id=1, history_summary=summary, extra_context=extra)
                for t in turns:
                    ctx.add_turn(t["role"], t["content"])
                legacy = ctx.digest()
                mine = render_window_for_decision(
                    turns, extra_context=extra, history_summary=summary
                )
                assert mine == legacy, (extra, bool(summary), len(turns))


def test_decision_window_truncation_matches_digest():
    from app.services.agents.lead.lead_agent import LeadContext

    ctx = LeadContext(user_id=1, history_summary="")
    ctx.turns = [{"role": "user", "content": "新" * 500}]
    legacy = ctx.digest(max_chars=120)
    mine = render_window_for_decision(
        [{"role": "user", "content": "新" * 500}], max_chars=120
    )
    assert mine == legacy
    assert mine.startswith("用户: 新") and mine.endswith("…")


# ── 回答窗口：select_for_answer == LeadContext.recent_turns ─────────────────

def test_answer_window_matches_recent_turns():
    from app.services.agents.lead.lead_agent import LeadContext

    turns = _turns(10)
    ctx = LeadContext(user_id=1)
    for t in turns:
        ctx.add_turn(t["role"], t["content"])
    legacy = ctx.recent_turns(max_turns=12, max_chars=2000)
    mine = select_for_answer(turns, max_turns=12, max_chars=2000)
    assert mine == legacy


def test_answer_window_budget_and_lone_answer():
    turns = [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "长" * 3000},
    ]
    got = select_for_answer(turns, max_turns=8, max_chars=500)
    assert sum(len(t["content"]) for t in got) <= 500
    assert got[-1]["content"].endswith("…")


# ── 状态四块：渲染 == lead_decider._build_context_blocks ────────────────────

def test_state_renderers_match_legacy_blocks():
    from app.services.agents.lead.lead_decider import _build_context_blocks

    datasources = [{"id": "ds1", "name": "销售", "type": "csv"}, {"id": "ds2", "name": "订单", "type": "postgres"}]
    subtasks = [
        "[子任务完成] 华东销售额柱状图（工具链 query_engine→add_chart_block）",
        "[子任务评估] 达标：success=True，已落块 1",
        "[子任务完成] 未决：华南数据缺失",
    ]
    layout = "画布当前共有 2 个块：图表 1 个、文本 1 个。"

    ds_block, canvas_block, assess_block, summary_block = _build_context_blocks(
        datasources, subtasks, layout
    )
    assert render_datasources(datasources) == ds_block
    assert render_datasources(None) == "（无）"                       # 空清单 → 占位
    assert render_canvas(layout) == canvas_block
    assert render_canvas("") == "（非画布入口或画布状态不可用）"        # 空 layout → 占位
    assert render_assessment(subtasks) == assess_block
    assert render_assessment([]) == "（本轮无子任务评估）"
    assert render_subtask_summaries(subtasks) == summary_block
    assert render_subtask_summaries(None) == "（本轮无已完成子任务）"


def test_subtask_summary_truncation_matches_legacy():
    from app.services.agents.lead.lead_decider import _build_context_blocks

    long_summary = "长" * 2000
    _, _, _, legacy = _build_context_blocks(None, [long_summary], "")
    assert render_subtask_summaries([long_summary]) == legacy
    assert legacy == f"- {long_summary[:1000]}"


# ── Provider 门面：端到端等价 ───────────────────────────────────────────────

def test_provider_for_decision_parts_match_digest():
    from app.services.agents.lead.lead_agent import LeadContext

    summary = "【口径与规则】销售额按全公司含税口径计算"
    turns = _turns(6)
    ctx = LeadContext(user_id="u1", session_id="s1", history_summary=summary)
    for t in turns:
        ctx.add_turn(t["role"], t["content"])

    provider = TurnContextProvider.for_turn(TurnInputs(
        user_id="u1", session_id="s1", entry="chat",
        turns=list(ctx.turns), history_summary=summary,
        datasources=[{"id": "ds1", "name": "销售", "type": "csv"}],
        metrics_ctx="sales_amount｜销售额",
        canvas_layout="",
    ))
    assembled = provider.for_decision("现在华南呢")

    assert assembled.parts["history_summary"] == ctx.digest()
    assert assembled.parts["datasources"] == "- id=ds1 name=销售 type=csv"
    assert assembled.parts["metrics"] == "sales_amount｜销售额"
    assert assembled.parts["user"] == "现在华南呢"
    assert assembled.ledger.used_tokens > 0
    assert "history_summary" in assembled.ledger.entries


def test_provider_for_answer_messages_match_legacy_answer_branch():
    from app.services.agents.lead.lead_agent import LeadContext, _ANSWER_SYSTEM

    summary = "【口径与规则】含税"
    turns = _turns(4)
    ctx = LeadContext(user_id="u1", session_id="s1", entry="canvas",
                      history_summary=summary, metrics_ctx="sales_amount｜销售额")
    for t in turns:
        ctx.add_turn(t["role"], t["content"])
    layout = "画布当前共有 2 个块：图表 1 个、文本 1 个。"

    # legacy：_answer_branch 的拼装顺序（system → 记忆 → 指标 → 画布 → recent_turns → user）
    legacy = [{"role": "system", "content": _ANSWER_SYSTEM}]
    mem = summary.strip()
    if mem:
        legacy.append({"role": "assistant", "content": f"【历史记忆】{mem}"})
    if ctx.metrics_ctx.strip():
        legacy.append({"role": "assistant", "content": f"【受治理指标清单】\n{ctx.metrics_ctx}"})
    if layout.strip():
        legacy.append({"role": "assistant", "content": f"【当前画布布局（已落盘，回答涉及画布时以此为准）】\n{layout}"})
    for t in ctx.recent_turns(max_turns=12):
        legacy.append({"role": t["role"], "content": t["content"]})
    legacy.append({"role": "user", "content": "画布上有什么"})

    provider = TurnContextProvider.for_turn(TurnInputs(
        user_id="u1", session_id="s1", entry="canvas",
        turns=list(ctx.turns), history_summary=summary,
        metrics_ctx=ctx.metrics_ctx, canvas_layout=layout,
    ))
    assembled = provider.for_answer("画布上有什么", system_text=_ANSWER_SYSTEM)
    assert assembled.messages == legacy


def test_provider_answer_empty_blocks_produce_minimal_messages():
    from app.services.agents.lead.lead_agent import _ANSWER_SYSTEM

    provider = TurnContextProvider.for_turn(TurnInputs(user_id="u1", entry="chat"))
    assembled = provider.for_answer("你好", system_text=_ANSWER_SYSTEM)
    assert assembled.messages == [
        {"role": "system", "content": _ANSWER_SYSTEM},
        {"role": "user", "content": "你好"},
    ]
    # 空画布在回答分支不注入占位（legacy _answer_branch 语义：layout 为空直接跳过）
    assert all("画布布局" not in str(m.get("content")) for m in assembled.messages)


def test_assembled_history_summary_reads_correct_key():
    """回归：Assembled.history_summary() 此前读错 key（window），永远返回空串。"""
    provider = TurnContextProvider.for_turn(TurnInputs(
        user_id="u1", entry="chat",
        turns=_turns(2), history_summary="【口径与规则】含税",
    ))
    assembled = provider.for_decision("看一眼")
    assert assembled.history_summary() != ""
    assert assembled.history_summary() == assembled.parts["history_summary"]


def test_for_answer_caps_long_memory_like_legacy():
    """等价性：for_answer 必须复刻 legacy 的记忆上限（LEAD_CTX_MAX_CHARS//3）。

    legacy（lead_agent._answer_branch）对超长摘要截到 4000//3=1333 字 + "…"，
    缺了这一步，长摘要场景新路径会比 legacy 多注入上千字。
    """
    from app.services.agents.lead.lead_agent import LeadContext, _ANSWER_SYSTEM

    long_mem = "记" * 3000
    ctx = LeadContext(user_id="u1", session_id="s1", history_summary=long_mem)
    provider = TurnContextProvider.for_turn(TurnInputs(
        user_id="u1", session_id="s1", entry="chat",
        turns=list(ctx.turns), history_summary=long_mem,
    ))
    assembled = provider.for_answer("你好", system_text=_ANSWER_SYSTEM)

    mem_cap = max(200, 4000 // 3)
    mem_msg = [m for m in assembled.messages if m.get("content", "").startswith("【历史记忆】")]
    assert len(mem_msg) == 1
    content = mem_msg[0]["content"]
    assert content == f"【历史记忆】{'记' * mem_cap}…"      # 与 legacy 截断逐字节一致
    assert "memory_capped_for_answer" in assembled.ledger.degraded


def test_for_answer_window_tokens_are_counted():
    """台账回归：WINDOW 是 messages 型块，tokens 不能记成 0。"""
    provider = TurnContextProvider.for_turn(TurnInputs(
        user_id="u1", entry="chat", turns=_turns(3),
    ))
    assembled = provider.for_answer("你好")
    assert assembled.ledger.entries.get("window", 0) > 0


def test_provider_refresh_canvas_updates_block():
    provider = TurnContextProvider.for_turn(TurnInputs(user_id="u1", entry="canvas", canvas_layout="旧"))
    before = provider.for_decision("看一眼")
    assert before.parts["canvas"] == "旧"
    provider.refresh_canvas("新布局")
    after = provider.for_decision("看一眼")
    assert after.parts["canvas"] == "新布局"
