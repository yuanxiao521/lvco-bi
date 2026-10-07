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


# ── 上下文装配：整条消息 / 预留活记忆 / 成对对齐 / 额外上下文 ──

def test_digest_keeps_whole_messages_instead_of_cutting_mid_sentence():
    """整条装配：装得下的消息必须完整保留，不出现"半句话"（旧实现从整体尾部硬切）。"""
    ctx = LeadContext(user_id=1, history_summary="")
    ctx.turns = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    ]
    text = ctx.digest(max_chars=4000)
    assert "用户: 第一问" in text and "助手: 第一答" in text
    assert "用户: 第二问" in text and "助手: 第二答" in text
    assert "…" not in text                      # 没有任何一条被截断


def test_digest_reserves_budget_for_live_turns():
    """长期记忆再长也要给活记忆留位置：不能把"刚才这几轮"整个挤掉。"""
    ctx = LeadContext(user_id=1, history_summary="旧" * 2000)
    ctx.turns = [
        {"role": "user", "content": "刚才问的问题"},
        {"role": "assistant", "content": "刚才的回答"},
    ]
    text = ctx.digest(max_chars=1000)
    assert "【长期记忆】" in text
    assert "刚才的回答" in text                  # 活记忆没被饿死
    assert len(text) <= 1000


def test_digest_truncates_only_the_oversized_newest_message():
    ctx = LeadContext(user_id=1, history_summary="")
    ctx.turns = [
        {"role": "user", "content": "旧问题"},
        {"role": "user", "content": "新" * 500},   # 单条自身超预算
    ]
    text = ctx.digest(max_chars=120)
    assert text.startswith("用户: 新")             # 保住最新那条的开头
    assert text.endswith("…")                      # 且明确标记被截断
    assert len(text) <= 120


def test_digest_drops_orphan_assistant_head():
    """裁剪后若以孤立的助手回答开头（没有配对的提问），丢掉这一条。"""
    ctx = LeadContext(user_id=1, history_summary="")
    ctx.turns = [
        {"role": "assistant", "content": "孤立回答"},
        {"role": "user", "content": "完整提问"},
        {"role": "assistant", "content": "完整回答"},
    ]
    # 三条都装得下，但对齐会丢掉开头的孤立回答
    text = ctx.digest(max_chars=2000)
    assert "孤立回答" not in text
    assert "用户: 完整提问" in text and "助手: 完整回答" in text


def test_digest_puts_extra_context_first():
    ctx = LeadContext(user_id=1, history_summary="长期记忆内容",
                      extra_context="【上一轮已生成的图表】柱状图")
    ctx.turns = [{"role": "user", "content": "重新画一个"}]
    text = ctx.digest(max_chars=2000)
    assert text.startswith("【上一轮已生成的图表】")
    assert "【长期记忆】" in text and "重新画一个" in text


def test_recent_turns_pair_aligned_and_budgeted():
    ctx = LeadContext(user_id=1)
    ctx.turns = [
        {"role": "assistant", "content": "孤立回答（窗口起点）"},
        {"role": "user", "content": "问1"},
        {"role": "assistant", "content": "答1"},
        {"role": "user", "content": "问2"},
        {"role": "assistant", "content": "答2"},
    ]
    got = ctx.recent_turns(max_turns=8, max_chars=500)
    assert [t["role"] for t in got] == ["user", "assistant", "user", "assistant"]
    assert got[0]["content"] == "问1"          # 开头的孤立回答被丢掉


def test_recent_turns_budget_and_keeps_lone_answer():
    ctx = LeadContext(user_id=1)
    ctx.turns = [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "长" * 3000},   # 单条超预算，且提问会被裁掉
    ]
    got = ctx.recent_turns(max_turns=8, max_chars=500)
    assert sum(len(t["content"]) for t in got) <= 500    # 预算封顶
    assert got and got[-1]["content"].endswith("…")      # 超长那条截断而非丢弃
    assert got[-1]["role"] == "assistant"                # 只剩回答时保留，不让活记忆变空


def test_build_chart_summary_note_respects_budget():
    """chart 摘要注入要有总量上限（旧实现按图累加且无上限，多图答案能灌进上万字符）。"""
    from app.api.v1.ai import _build_chart_summary_note

    class _Msg:
        def __init__(self, role, charts):
            self.role = role
            self.chart_data = {"charts": charts} if charts else None

    mk = lambda n: [{"chart_type": "bar", "option": {"title": "长" * n}}]
    msgs = [
        _Msg(AIMessageRole.user, None),
        _Msg(AIMessageRole.assistant, mk(200)),
        _Msg(AIMessageRole.assistant, mk(200)),
        _Msg(AIMessageRole.assistant, mk(200)),
    ]
    full = _build_chart_summary_note(msgs, 99999)
    assert full.count("- bar 图表：") == 3                     # 都在预算内时全带上

    capped = _build_chart_summary_note(msgs, 500)              # 预算收紧
    assert capped and len(capped) <= 500 + 40                  # 头部提示语之外的正文受控
    assert capped.count("- bar 图表：") < 3                    # 超出预算即停
    assert _build_chart_summary_note([], 1000) == ""           # 无图不给注入


def test_estimate_tokens_cjk_vs_ascii():
    from app.services.context_utils import estimate_tokens

    assert estimate_tokens("") == 0
    assert estimate_tokens("中文四字") == 4                 # CJK 1 字≈1 token
    assert estimate_tokens("abcdefgh") == 2                # 非 CJK 4 字符≈1 token
    assert estimate_tokens("中文abcd") == 3


def test_align_history_pairs_drops_leading_assistant():
    from app.services.context_utils import align_history_pairs

    rows = [
        {"role": "assistant", "content": "孤立回答"},
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]
    assert align_history_pairs(rows) == rows[1:]
    assert align_history_pairs([]) == []
    # 末尾的孤立 user（被中断的那轮提问）保留：它本来就是待办
    tail_user = [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"},
                 {"role": "user", "content": "又问"}]
    assert align_history_pairs(tail_user) == tail_user


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


def test_filter_canvas_history_drops_orphan_assistant_head():
    """窗口起点落在某轮回答上（多取的那 1 条）→ 对齐后丢掉孤立回答。"""
    rows = [
        _FakeMsg(AIMessageRole.assistant, "上一轮的回答（窗口起点，孤立）", 1),
        _FakeMsg(AIMessageRole.user, "本轮之前的提问", 2),
        _FakeMsg(AIMessageRole.assistant, "本轮之前的回答", 3),
        _FakeMsg(AIMessageRole.user, "本条提问", 4),
    ]
    out = _filter_canvas_history(rows, current_msg_id=4)
    assert [m["content"] for m in out] == ["本轮之前的提问", "本轮之前的回答"]


# ── _maybe_summarize：阈值触发 / 进度推进 / 失败不推进（核心编排逻辑）──

class _FakeLLM:
    def __init__(self, reply="合并后的摘要", exc=None):
        self.reply = reply
        self.exc = exc
        self.calls = 0
        self.seen: list[list[dict]] = []
        self.kwargs: list[dict] = []

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.seen.append(messages)
        self.kwargs.append(kwargs)
        if self.exc:
            raise self.exc
        return self.reply


def _patch_merge_io(monkeypatch, total_rounds: int, messages: list[dict]):
    """把 DB 侧两个 IO 替换成定值（合并编排逻辑与 DB 无关，便于纯内存验证）。

    模拟真实 `_load_unmerged_messages` 的语义：返回的消息列表跳过空内容行，但
    "本段最后一条消息 id"取**原始窗口最后一行**（空行也要被水位跨越）。
    """
    from app.services.agents.lead import lead_agent as mod

    rows = [
        {"id": m.get("id") or f"msg-{i}", "role": m["role"], "content": m["content"]}
        for i, m in enumerate(messages, 1)
    ]
    kept = [r for r in rows if str(r["content"]).strip()]

    async def _count(db_session, session_id):
        return total_rounds

    async def _load(db_session, session_id, after_id=None, max_messages=24):
        return list(kept), (rows[-1]["id"] if rows else None)

    monkeypatch.setattr(mod, "_count_session_user_rounds", _count)
    monkeypatch.setattr(mod, "_load_unmerged_messages", _load)
    return rows


async def test_maybe_summarize_skips_below_threshold(monkeypatch):
    _patch_merge_io(monkeypatch, 3, [{"role": "user", "content": "a"}])
    llm = _FakeLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=0)
    assert await agent._maybe_summarize(ctx, db_session=object()) is None
    assert llm.calls == 0                 # 未达阈值不产生额外 LLM 调用
    assert ctx.memory_covered == 0


async def test_maybe_summarize_merges_and_advances_progress(monkeypatch):
    rows = _patch_merge_io(monkeypatch, 4, [
        {"role": "user", "content": "华东销售额多少"},
        {"role": "assistant", "content": "8,640 万"},
        {"role": "user", "content": "那华南呢"},
        {"role": "assistant", "content": "3,120 万"},
        {"role": "user", "content": "好"},
    ])
    llm = _FakeLLM("<analysis>草稿</analysis><summary>口径：全公司含税；华东 8,640 万</summary>")
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", history_summary="旧：口径=含税",
                      memory_covered=0)
    ev = await agent._maybe_summarize(ctx, db_session=object())
    assert llm.calls == 1
    assert ev["source"] == "lead"
    assert ev["summary"] == "口径：全公司含税；华东 8,640 万"      # <analysis> 草稿被剥掉
    assert ev["covered_rounds"] == 3                              # 诚实计数：实际并入 3 个用户轮
    assert ev["last_merged_message_id"] == rows[-1]["id"]         # 水位推进到本段最后一条
    assert ctx.memory_watermark == rows[-1]["id"]
    assert ctx.memory_covered == 3


async def test_maybe_summarize_prompt_keeps_old_memory(monkeypatch):
    """保旧纳新：合并输入必须带上【已有长期记忆】，否则就退化成窗口重算。"""
    _patch_merge_io(monkeypatch, 4, [
        {"role": "user", "content": "新问题"},
        {"role": "user", "content": "再问一句"},
        {"role": "user", "content": "还有"},
        {"role": "user", "content": "最后"},
    ])
    llm = _FakeLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", history_summary="旧口径：全公司含税",
                      memory_covered=0)
    await agent._maybe_summarize(ctx, db_session=object())
    user_content = llm.seen[0][-1]["content"]
    assert "【已有长期记忆】" in user_content and "旧口径：全公司含税" in user_content
    assert "【新增对话】" in user_content


async def test_maybe_summarize_llm_failure_keeps_progress(monkeypatch):
    """失败不脏数据：不写摘要、不推进水位，只上报失败（供熔断计数）。"""
    _patch_merge_io(monkeypatch, 4, [{"role": "user", "content": "a"}])
    llm = _FakeLLM(exc=RuntimeError("boom"))
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", history_summary="旧记忆",
                      memory_covered=0)
    ev = await agent._maybe_summarize(ctx, db_session=object())
    assert ev == {"source": "lead", "failed": True}
    assert ctx.memory_covered == 0 and ctx.memory_watermark is None


async def test_maybe_summarize_empty_reply_keeps_progress(monkeypatch):
    """空摘要同样按失败处理，避免把空内容写进长期记忆。"""
    _patch_merge_io(monkeypatch, 5, [{"role": "user", "content": "a"}])
    llm = _FakeLLM("   ")
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=1)
    assert await agent._maybe_summarize(ctx, db_session=object()) == {
        "source": "lead", "failed": True
    }
    assert ctx.memory_watermark is None


async def test_maybe_summarize_circuit_breaker(monkeypatch):
    """熔断：连续失败达阈值后不再重试（不再每轮白烧一次 LLM 调用）。"""
    _patch_merge_io(monkeypatch, 8, [{"role": "user", "content": "a"}])
    llm = _FakeLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_fail_count=3)
    assert await agent._maybe_summarize(ctx, db_session=object()) is None
    assert llm.calls == 0


async def test_maybe_summarize_empty_segment_only_advances_watermark(monkeypatch):
    """本段全是空占位：不花 LLM，只把水位推过去（否则会永远卡在这一段上）。"""
    rows = _patch_merge_io(monkeypatch, 5, [
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": "   "},
    ])
    llm = _FakeLLM()
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=1)
    ev = await agent._maybe_summarize(ctx, db_session=object())
    assert llm.calls == 0
    assert ev["summary"] is None
    assert ev["last_merged_message_id"] == rows[-1]["id"]
    assert ctx.memory_watermark == rows[-1]["id"]


def test_memory_merge_prompt_is_structured_and_configurable(monkeypatch):
    """摘要必须分区化：口径有固定位置（不会被新信息挤掉）、数字要求带语境（可溯源）。"""
    from app.config import settings
    from app.services.agents.lead.lead_agent import _memory_merge_system_prompt

    prompt = _memory_merge_system_prompt()
    for sec in ("【口径与规则】", "【关键数字与结论】", "【数据源与字段】", "【未决问题】"):
        assert sec in prompt
    assert "时间范围/口径/来源" in prompt      # 数字必须带语境
    assert "逐条保留" in prompt               # 旧口径不得被新信息挤掉
    monkeypatch.setattr(settings, "LEAD_MEMORY_SUMMARY_CHARS", 1234)
    assert "1234" in _memory_merge_system_prompt()   # 字数上限可配


async def test_maybe_summarize_keeps_structured_summary_intact(monkeypatch):
    """分区化摘要（口径 / 数字+范围+来源 / 数据源 / 未决）必须原样落库，不被截断。"""
    _patch_merge_io(monkeypatch, 5, [
        {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ])
    body = (
        "【口径与规则】销售额按全公司含税口径计算\n"
        "【关键数字与结论】华东销售额 8,640 万（含税口径，2026Q3，Ecommerce Orders）\n"
        "【数据源与字段】Ecommerce Orders（region / total_amount）\n"
        "【未决问题】（无）"
    )
    llm = _FakeLLM(f"<analysis>草稿</analysis><summary>{body}</summary>")
    agent = LeadAgent(llm=llm)
    ctx = LeadContext(user_id=1, session_id="s1", memory_covered=0)
    ev = await agent._maybe_summarize(ctx, db_session=object())
    assert ev["summary"] == body                                    # 四节完整保留
    assert "8,640 万（含税口径，2026Q3，Ecommerce Orders）" in ev["summary"]
    assert llm.kwargs[0]["max_tokens"] >= 900                       # 给分区摘要留足输出空间


def test_extract_summary_strips_analysis_draft():
    from app.services.agents.lead.lead_agent import _extract_summary

    assert _extract_summary("<analysis>草稿</analysis><summary>正文</summary>") == "正文"
    assert _extract_summary("没有标签的纯文本") == "没有标签的纯文本"
    assert _extract_summary("") == ""