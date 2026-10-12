# -*- coding: utf-8 -*-
"""阶段 3 回归：窗口所有权归位 / 合并触发与窗口联动 / 熔断规则式兜底。

锁定三条新语义：
1. 窗口联动——存在"已滑出窗口且未并入"的轮次时，合并**必须**触发（即使未到 min_rounds）；
2. 熔断兜底——LLM 熔断期间，滑出窗口的轮次用规则式摘要保存，不产生空心窗口；
3. 服务端窗口——_load_server_window 的选点/排除/对齐规则。
"""
import pytest

from app.config import settings
from app.services.agents.lead.lead_agent import (
    LeadAgent,
    LeadContext,
    _resolve_memory_progress,
    _rule_based_fallback_summary,
    _should_merge_memory,
)


# ── 合并触发与窗口联动 ──────────────────────────────────────────────────────

def test_merge_trigger_threshold_semantics_unchanged():
    """未给窗口时保持旧语义（节流阈值），保证向后兼容。"""
    assert _should_merge_memory(4, 0, 4) is True
    assert _should_merge_memory(3, 0, 4) is False
    assert _should_merge_memory(7, 3, 4) is True


def test_merge_trigger_couples_to_window():
    """核心新语义：窗口外有未并入轮次 → 必须触发（即使 unmerged < min_rounds）。"""
    # 窗口 8 轮：unmerged=9 > 8 → 触发（即使阈值是 6，9 >= 6 本来也会触发；用小阈值验证）
    assert _should_merge_memory(9, 0, 6, window_rounds=8) is True
    # unmerged=9、阈值 10（正常节流不触发）→ 窗口联动强制触发
    assert _should_merge_memory(9, 0, 10, window_rounds=8) is True
    # unmerged=8（= 窗口，未滑出）→ 不强制，按阈值
    assert _should_merge_memory(8, 0, 10, window_rounds=8) is False
    # unmerged=7 < 窗口 → 不强制
    assert _should_merge_memory(7, 0, 10, window_rounds=8) is False


def test_merge_trigger_window_coupled_with_progress():
    # 已并入 2 轮、总 10 轮、窗口 8：unmerged=8 = 窗口 → 不强制
    assert _should_merge_memory(10, 2, 10, window_rounds=8) is False
    # 已并入 1 轮、总 10 轮、窗口 8：unmerged=9 > 窗口 → 强制
    assert _should_merge_memory(10, 1, 10, window_rounds=8) is True


def test_dirty_progress_defense_still_applies_with_window():
    """脏数据防御与窗口联动共存：covered 越界归零后按归零值判定。"""
    assert _resolve_memory_progress(9, 999) == 0
    assert _should_merge_memory(9, 999, 10, window_rounds=8) is True


# ── 熔断规则式兜底摘要 ──────────────────────────────────────────────────────

def test_fallback_summary_extracts_user_lines_and_numbers():
    msgs = [
        {"role": "user", "content": "华东销售额多少？要含税口径"},
        {"role": "assistant", "content": "华东销售额 8,640 万（含税，2026Q3）"},
        {"role": "user", "content": "那华南呢"},
        {"role": "assistant", "content": "华南 3,120 万，订单 1.2万 条"},
    ]
    out = _rule_based_fallback_summary(msgs)
    assert "【口径与规则】" in out
    assert "华东销售额多少" in out              # 用户首句被保存
    assert "那华南呢" in out
    assert "8,640 万" in out                    # 数字 token 被保存
    assert "3,120 万" in out                    # 带量纲的数字
    assert "1.2万" in out                       # 量纲单字（"条"跟在后面，正则取第一个量纲）


def test_fallback_summary_empty_segment():
    assert _rule_based_fallback_summary([]) == "（本段无可并入内容）"
    assert _rule_based_fallback_summary([{"role": "user", "content": "  "}])


def test_fallback_summary_long_user_line_truncated():
    out = _rule_based_fallback_summary([{"role": "user", "content": "长" * 200}])
    assert len(out) < 200                       # 首句被截到 80 字


# ── 熔断兜底端到端：_maybe_summarize 在熔断 + 窗口外有积压时产出兜底摘要 ─────

def _patch_merge_io(monkeypatch, total_rounds: int, messages: list[dict]):
    from app.services.agents.lead import lead_agent as mod

    monkeypatch.setattr(settings, "WINDOW_MAX_ROUNDS", 8)

    rows = [
        {"id": f"msg-{i}", "role": m["role"], "content": m["content"]}
        for i, m in enumerate(messages, 1)
    ]
    kept = [r for r in rows if str(r["content"]).strip()]

    async def _count(db_session, session_id):
        return total_rounds

    async def _load(db_session, session_id, after_id=None, max_messages=24):
        return list(kept), (rows[-1]["id"] if rows else None)

    monkeypatch.setattr(mod, "_count_session_user_rounds", _count)
    monkeypatch.setattr(mod, "_load_unmerged_messages", _load)


async def test_circuit_open_with_gap_produces_fallback(monkeypatch):
    """熔断 + 窗口外积压 → 规则式兜底（不再返回 None 留空洞）。"""
    _patch_merge_io(monkeypatch, 20, [
        {"role": "user", "content": "第2轮问题 华东 8,640 万"},
        {"role": "assistant", "content": "答 8,640 万"},
    ])
    llm_calls = {"n": 0}

    class _LLM:
        async def complete(self, *a, **kw):
            llm_calls["n"] += 1
            return "不应被调用"

    agent = LeadAgent(llm=_LLM())
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=10, memory_fail_count=3)
    ev = await agent._maybe_summarize(ctx, db_session=object())

    assert ev is not None and ev.get("summary")
    assert "第2轮问题" in ev["summary"]
    assert llm_calls["n"] == 0                          # 熔断期不烧 LLM
    assert ctx.memory_watermark == "msg-2"              # 水位推进，无空洞
    # 诚实计数：mock 只给 1 个用户轮 → covered = min(10+1, 20-1) = 11
    assert ctx.memory_covered == 11


async def test_circuit_open_without_gap_returns_none(monkeypatch):
    """熔断但窗口内没有空洞（unmerged <= window）→ 保持旧行为跳过。"""
    _patch_merge_io(monkeypatch, 18, [{"role": "user", "content": "a"}])
    agent = LeadAgent(llm=None)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=10, memory_fail_count=3)
    # unmerged = 8 = 窗口 → 未滑出 → None
    assert await agent._maybe_summarize(ctx, db_session=object()) is None
