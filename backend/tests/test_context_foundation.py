# -*- coding: utf-8 -*-
"""四件套基础层回归：token 口径换算 / 压缩策略与旧实现等价 / 台账。

P0 的核心承诺是**行为零变化**，所以本文件最重要的一组断言是
"新策略 == 旧实现逐字节相同"——它就是阶段 1 的安全网。
"""
from app.config import settings
from app.services.context.budgets import (
    TARGET_ANSWER,
    TARGET_DECISION,
    TOKENS_PER_CHAR_JSON,
    TOKENS_PER_CHAR_MIXED,
    TOKENS_PER_CHAR_ZH,
    active_table,
    chars_for_tokens,
    equivalent_worker_table,
    tokens_for_chars,
)
from app.services.context.compress import (
    DEGRADE_MESSAGE_LIST_FOLDED,
    DEGRADE_TOOL_COMPACTED,
    Compressor,
    MessageListPolicy,
    PassThroughPolicy,
    ToolResultPolicy,
)
from app.services.context.types import Block, BlockKind, Ledger, Priority
from app.services.context_utils import compact_result_json, compress_history


# ── token 口径：等价换算必须精确往返（否则 P0 就不是零行为变化） ──────────────

def test_token_char_roundtrip_is_exact():
    cases = [
        (2000, TOKENS_PER_CHAR_JSON),    # RESULT_MAX_CHARS
        (8000, TOKENS_PER_CHAR_JSON),    # RESULT_MAX_META_CHARS
        (4000, TOKENS_PER_CHAR_MIXED),   # LEAD_CTX_MAX_CHARS
        (2000, TOKENS_PER_CHAR_ZH),      # LEAD_CTX_ANSWER_HISTORY_CHARS
        (600, TOKENS_PER_CHAR_ZH),       # LEAD_MEMORY_SUMMARY_CHARS
        (150000, TOKENS_PER_CHAR_MIXED),  # CONTEXT_MAX_CHARS
    ]
    for chars, ratio in cases:
        tok = tokens_for_chars(chars, ratio)
        assert chars_for_tokens(tok, ratio) == chars, (chars, ratio, tok)


def test_equivalent_worker_table_matches_legacy_chars():
    b = equivalent_worker_table().get(BlockKind.TOOL_RESULT)
    assert b is not None
    assert chars_for_tokens(b.max_tokens, TOKENS_PER_CHAR_JSON) == settings.RESULT_MAX_CHARS


def test_target_table_totals():
    assert TARGET_DECISION.input_budget() == 4050
    assert TARGET_ANSWER.input_budget() == 2600
    # P0 未强制生效：等价表与目标表必须能区分开，避免"看起来已经改了"
    assert active_table("decision").enforced is False
    assert active_table("decision").purpose == "decision"


# ── Block / Ledger 基础语义 ────────────────────────────────────────────────

def test_block_tokens_text_and_messages():
    assert Block(kind=BlockKind.MEMORY, text="中文四字").tokens() == 4
    msgs = ({"role": "user", "content": "中文四字"}, {"role": "assistant", "content": "abcd"})
    blk = Block(kind=BlockKind.WINDOW, messages=msgs)
    assert blk.tokens() == 4 + 1
    assert Block(kind=BlockKind.MEMORY).is_empty is True


def test_ledger_records_drops_and_budget():
    led = Ledger(purpose="decision", budget_tokens=100)
    led.record("memory", 40)
    led.record("memory", 30)
    led.record("window", 50)
    assert led.entries == {"memory": 70, "window": 50}
    assert led.used_tokens == 120
    assert led.over_budget is True
    led.drop("subtask")
    led.drop("subtask")          # 去重
    led.degrade("window_truncated")
    led.degrade("window_truncated")
    assert led.dropped == ["subtask"] and led.degraded == ["window_truncated"]
    d = led.as_dict()
    assert d["used_tokens"] == 120 and d["over_budget"] is True
    assert "OVER" in led.summary()


def test_priority_order_means_near_content_survives():
    # 越近越贵：子任务摘要先丢，窗口最后丢
    assert Priority.SUBTASK < Priority.ASSESSMENT < Priority.METRICS < Priority.MEMORY < Priority.WINDOW
    assert Priority.USER > Priority.WINDOW


# ── 压缩策略：与旧实现逐字节等价（P0 安全网） ───────────────────────────────

def _big_rows_json(n: int = 80) -> str:   # 需超过 RESULT_MAX_CHARS(2000) 才会触发压缩
    import json
    return json.dumps({
        "executed_sql": "SELECT region, SUM(amount) FROM t GROUP BY region",
        "columns": ["region", "amount"],
        "row_count": n,
        "rows": [{"region": f"region-{i}", "amount": i * 1000} for i in range(n)],
        "summary": {"columns_count": 2, "rows_count": n},
    }, ensure_ascii=False)


def test_tool_result_policy_equivalent_to_legacy():
    text = _big_rows_json()
    assert len(text) > settings.RESULT_MAX_CHARS      # 前置：确实需要压缩
    budget = equivalent_worker_table().get(BlockKind.TOOL_RESULT).max_tokens
    new_block, reason = ToolResultPolicy().compress(Block(kind=BlockKind.TOOL_RESULT, text=text), budget)
    legacy = compact_result_json(text)                # 旧调用点默认参数
    assert new_block.text == legacy
    assert reason == DEGRADE_TOOL_COMPACTED


def test_tool_result_policy_keeps_error_intact():
    import json
    err = json.dumps({"error": "column not found", "hint": "可用列: " + "x" * 5000}, ensure_ascii=False)
    assert len(err) > settings.RESULT_MAX_CHARS
    block, reason = ToolResultPolicy().compress(Block(kind=BlockKind.TOOL_RESULT, text=err), 800)
    assert block.text == err                          # error 完整保留
    assert reason is None


def test_tool_result_policy_short_text_untouched():
    text = '{"rows": [{"a": 1}]}'
    block, reason = ToolResultPolicy().compress(Block(kind=BlockKind.TOOL_RESULT, text=text), 800)
    assert block.text == text and reason is None


def test_message_list_policy_equivalent_to_legacy():
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(120):
        msgs.append({"role": "user", "content": f"问{i}" + "字" * 50})
        msgs.append({"role": "assistant", "content": f"答{i}" + "字" * 50})
    budget = equivalent_worker_table().get(BlockKind.WINDOW).max_tokens
    policy = MessageListPolicy(keep=40, fallback_chars=settings.CONTEXT_MAX_CHARS)
    block, reason = policy.compress(Block(kind=BlockKind.WINDOW, messages=tuple(msgs)), budget)
    legacy = compress_history([dict(m) for m in msgs], keep=40, max_chars=settings.CONTEXT_MAX_CHARS)
    assert [dict(m) for m in block.messages] == legacy
    # 关键发现：compress_history 的折叠由**条数**（keep）触发，不只看字符预算——
    # 总字符数远小于上限时依然会折叠。新策略必须复现这一点，所以 reason 非空。
    assert reason == DEGRADE_MESSAGE_LIST_FOLDED
    assert len(legacy) < len(msgs)


def test_message_list_policy_folds_when_budget_is_tight():
    msgs = [{"role": "user", "content": f"问{i}" + "字" * 50} for i in range(200)]
    block, reason = MessageListPolicy(keep=5).compress(
        Block(kind=BlockKind.WINDOW, messages=tuple(msgs)), 200
    )
    assert len(block.messages) < len(msgs)
    assert reason == DEGRADE_MESSAGE_LIST_FOLDED


def test_message_list_policy_passes_text_block_through():
    blk = Block(kind=BlockKind.WINDOW, text="渲染后的窗口文本")
    out, reason = MessageListPolicy().compress(blk, 100)
    assert out is blk and reason is None


# ── Compressor 分派 ────────────────────────────────────────────────────────

def test_compressor_dispatches_by_kind_and_defaults_to_passthrough():
    comp = Compressor()
    assert isinstance(comp.policy_for(BlockKind.TOOL_RESULT), ToolResultPolicy)
    assert isinstance(comp.policy_for(BlockKind.WINDOW), MessageListPolicy)
    assert isinstance(comp.policy_for(BlockKind.CANVAS), PassThroughPolicy)

    blk = Block(kind=BlockKind.CANVAS, text="画布布局")
    out, reason = comp.compress(blk, 10)
    assert out is blk and reason is None


def test_compressor_skips_uncompressible_blocks():
    comp = Compressor()
    text = _big_rows_json()
    out, reason = comp.compress(Block(kind=BlockKind.TOOL_RESULT, text=text, compressible=False), 10)
    assert out.text == text and reason is None


# ── 阶段 4：StructuredMemoryPolicy 四节分区配额 ─────────────────────────────

def _four_section_memory(pad: int = 0) -> str:
    return (
        "【口径与规则】销售额按全公司含税口径计算\n"
        + "口" * pad
        + "\n【关键数字与结论】华东 8,640 万（含税，2026Q3）\n"
        "【数据源与字段】Ecommerce Orders（region / total_amount）\n"
        "【未决问题】华南数据源未接入，待用户确认"
    )


def test_memory_policy_keeps_all_four_sections():
    """核心语义：分区裁剪后四节都还在——旧实现的尾部硬切会整节砍掉【未决问题】。"""
    from app.services.context.compress import StructuredMemoryPolicy
    from app.services.context.types import Block, BlockKind

    long_summary = _four_section_memory(pad=2000)     # 超长摘要
    block, reason = StructuredMemoryPolicy().compress(
        Block(kind=BlockKind.MEMORY, text=long_summary), 600
    )
    for sec in ("【口径与规则】", "【关键数字与结论】", "【数据源与字段】", "【未决问题】"):
        assert sec in block.text, sec                 # 每节都活着
    assert reason == "memory_section_capped"


def test_memory_policy_noop_within_budget():
    from app.services.context.compress import StructuredMemoryPolicy
    from app.services.context.types import Block, BlockKind

    short = _four_section_memory(pad=0)
    block, reason = StructuredMemoryPolicy().compress(
        Block(kind=BlockKind.MEMORY, text=short), 600
    )
    assert block.text == short and reason is None


def test_memory_policy_plain_text_falls_back_to_hard_cap():
    """legacy 纯文本摘要（无节标记）→ 整体硬切兜底。"""
    from app.services.context.compress import StructuredMemoryPolicy
    from app.services.context.types import Block, BlockKind

    plain = "记" * 3000
    block, reason = StructuredMemoryPolicy().compress(
        Block(kind=BlockKind.MEMORY, text=plain), 600
    )
    assert block.text.startswith("记") and block.text.endswith("…")
    assert reason == "memory_section_capped"


def test_compressor_measure_matches_estimate_tokens():
    from app.services.context_utils import estimate_tokens
    assert Compressor.measure("中文四字") == estimate_tokens("中文四字") == 4
    assert Compressor.measure(Block(kind=BlockKind.MEMORY, text="abcdefgh")) == 2
