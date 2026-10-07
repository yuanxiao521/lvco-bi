# -*- coding: utf-8 -*-
"""记忆链路修复回归：累积合并节流 / 合并输入组装 / digest 截断方向 / 画布历史过滤。

修复背景：Lead 记忆回流此前是"每请求窗口重算 + 覆盖写"（旧摘要不参与），长会话失忆；
画布历史加载误跳全部 assistant，且当前用户消息在窗口中重复。本文件锁住修复后的行为。
"""
from app.api.v1.ai import _filter_canvas_history
from app.models.ai_message import AIMessageRole
from app.services.agents.lead.lead_agent import (
    LeadAgent,
    LeadContext,
    _build_memory_merge_input,
    _resolve_memory_progress,
    _should_merge_memory,
)


# ── 节流判定与进度防御 ──

def test_should_merge_memory_throttle():
    assert _should_merge_memory(4, 0, 4) is True
    assert _should_merge_memory(3, 0, 4) is False
    assert _should_merge_memory(6, 3, 4) is False
    assert _should_merge_memory(7, 3, 4) is True
    assert _should_merge_memory(0, 0, 4) is False


def test_memory_progress_dirty_data_defense():
    # 旧语义脏数据（covered 越界）→ 归零，最多多合并一次，不丢内容
    assert _resolve_memory_progress(5, 999) == 0
    assert _resolve_memory_progress(5, -2) == 0
    assert _resolve_memory_progress(5, 3) == 3
    assert _should_merge_memory(5, 999, 4) is True


# ── 合并输入组装（旧记忆 + 新段）──

def test_build_memory_merge_input_keeps_old_and_new():
    text = _build_memory_merge_input("口径：全公司含税", [
        {"role": "user", "content": "华东销售额多少"},
        {"role": "assistant", "content": "8,640 万"},
        {"role": "assistant", "content": "   "},
    ])
    assert "【已有长期记忆】" in text and "全公司含税" in text
    assert "【新增对话】" in text
    assert "用户: 华东销售额多少" in text
    assert text.count("助手:") == 1  # 空内容消息被跳过


def test_build_memory_merge_input_empty_old():
    text = _build_memory_merge_input("", [{"role": "user", "content": "你好"}])
    assert "【已有长期记忆】" not in text
    assert "【新增对话】" in text


def test_build_memory_merge_input_truncates():
    long_line = "长" * 2000
    text = _build_memory_merge_input(
        "旧记忆", [{"role": "user", "content": long_line}],
        per_msg_limit=100, total_limit=150,
    )
    assert "长" * 100 in text
    assert "长" * 101 not in text


# ── digest：截断时保住【长期记忆】──

def test_digest_keeps_long_term_memory_head_on_truncate():
    ctx = LeadContext(user_id=1, history_summary="口径：全公司含税。" * 20)
    ctx.turns = [{"role": "user", "content": "很长的最近对话" * 100}]
    text = ctx.digest(max_chars=300)
    assert "【长期记忆】" in text  # 修复点：不再从头部截断丢掉长期记忆
    assert len(text) <= 300


def test_digest_without_memory_falls_back_to_tail():
    ctx = LeadContext(user_id=1)
    ctx.turns = [{"role": "user", "content": "旧内容" * 200}]
    text = ctx.digest(max_chars=100)
    assert len(text) == 100


# ── 画布历史过滤 ──

class _FakeMsg:
    def __init__(self, role, content, msg_id=None):
        self.role = role
        self.content = content
        self.id = msg_id


def test_filter_canvas_history_skips_only_empty_placeholder():
    rows = [
        _FakeMsg(AIMessageRole.user, "上一条提问", 1),
        _FakeMsg(AIMessageRole.assistant, "分析完成，已生成 2 个内容块", 2),
        _FakeMsg(AIMessageRole.assistant, "", 3),
        _FakeMsg(AIMessageRole.user, "本条提问", 4),
    ]
    out = _filter_canvas_history(rows, current_msg_id=4)
    assert [m["content"] for m in out] == ["上一条提问", "分析完成，已生成 2 个内容块"]


def test_filter_canvas_history_without_current_id():
    rows = [_FakeMsg(AIMessageRole.user, "a", None)]
    out = _filter_canvas_history(rows, None)
    assert out == [{"role": "user", "content": "a"}]


# ── _maybe_summarize：阈值触发 / 进度推进 / 失败不推进（核心编排逻辑）──

class _FakeLLM:
    def __init__(self, reply="合并后的摘要", exc=None):
        self.reply = reply
        self.exc = exc
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.seen.append(messages)
        if self.exc:
            raise self.exc
        return self.reply


def _patch_merge_io(monkeypatch, total_rounds: int, messages: list[dict]):
    """把 DB 侧两个 IO 替换成定值（合并编排逻辑与 DB 无关，便于纯内存验证）。"""
    from app.services.agents.lead import lead_agent as mod

    async def _count(db_session, session_id):
        return total_rounds

    async def _load(db_session, session_id, new_rounds):
        return messages

    monkeypatch.setattr(mod, "_count_session_user_rounds", _count)
    monkeypatch.setattr(mod, "_load_unmerged_messages", _load)


async def test_maybe_summarize_skips_below_threshold(monkeypatch):
    _patch_merge_io(monkeypatch, 3, [{"role": "user", "content": "a"}])
    llm = _FakeLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=0)
    assert await agent._maybe_summarize(ctx, db_session=object()) is None
    assert llm.calls == 0                 # 未达阈值不产生额外 LLM 调用
    assert ctx.memory_covered == 0


async def test_maybe_summarize_merges_and_advances_progress(monkeypatch):
    _patch_merge_io(monkeypatch, 4, [
        {"role": "user", "content": "华东销售额多少"},
        {"role": "assistant", "content": "8,640 万"},
    ])
    llm = _FakeLLM("口径：全公司含税；华东 8,640 万")
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", history_summary="旧：口径=含税",
                      memory_covered=0)
    ev = await agent._maybe_summarize(ctx, db_session=object())
    assert llm.calls == 1
    # 最新一轮留待下次（本轮助手产出尚未落库），进度停在 total-1
    assert ev == {"summary": "口径：全公司含税；华东 8,640 万", "covered_rounds": 3}
    assert ctx.memory_covered == 3


async def test_maybe_summarize_prompt_keeps_old_memory(monkeypatch):
    """保旧纳新：合并输入必须带上【已有长期记忆】，否则就是退化成窗口重算。"""
    _patch_merge_io(monkeypatch, 4, [{"role": "user", "content": "新问题"}])
    llm = _FakeLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", history_summary="旧口径：全公司含税",
                      memory_covered=0)
    await agent._maybe_summarize(ctx, db_session=object())
    user_content = llm.seen[0][-1]["content"]
    assert "【已有长期记忆】" in user_content and "旧口径：全公司含税" in user_content
    assert "【新增对话】" in user_content


async def test_maybe_summarize_llm_failure_keeps_progress(monkeypatch):
    """失败不脏数据：LLM 异常时不写记忆、不推进进度，下一轮自然重试。"""
    _patch_merge_io(monkeypatch, 4, [{"role": "user", "content": "a"}])
    llm = _FakeLLM(exc=RuntimeError("boom"))
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", history_summary="旧记忆",
                      memory_covered=0)
    assert await agent._maybe_summarize(ctx, db_session=object()) is None
    assert ctx.memory_covered == 0


async def test_maybe_summarize_empty_reply_keeps_progress(monkeypatch):
    _patch_merge_io(monkeypatch, 5, [{"role": "user", "content": "a"}])
    llm = _FakeLLM("   ")
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=1)
    assert await agent._maybe_summarize(ctx, db_session=object()) is None
    assert ctx.memory_covered == 1