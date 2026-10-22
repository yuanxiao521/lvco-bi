# -*- coding: utf-8 -*-
"""P4 / §5.3：工具结果进压缩——循环里唯一"随步数增长"的块必须有上限。

为什么必须补这一环：
  · 状态块（记忆/画布/数据源/指标）每步内容恒定，真正会膨胀的是**工具结果**——
    每派发一步就追加一组 assistant(tool_calls)+tool 消息，步数越多 prompt 越大；
  · §7 风险表写得很清楚：「上下文膨胀 → TOOL_RESULT 进 Compressor」。

三条设计约束（本文件逐条锁定）：
  1. **只动 content**：role / tool_call_id / assistant.tool_calls 原样保留，
     工具协议配对不可破坏（错位 = 下一次调用 400）；
  2. **确定性**：同一条结果在任何一步压出的文本都相同——不会因为"后面又来了两条"
     而改变已发出去的内容，前缀缓存因此不吃亏；
  3. **复用既有口径**：compact_result_json（error 完整保留、rows 只留前 N 行），
     不在装配层另写一套截断规则。
"""
from __future__ import annotations

import json

from app.services.context.assembler import Assembler, compress_tool_message
from app.services.context.budgets import TOKENS_PER_CHAR_JSON, chars_for_tokens
from app.services.context.types import Block, BlockKind

BUDGET_TOKENS = 800
CHAR_BUDGET = chars_for_tokens(BUDGET_TOKENS, TOKENS_PER_CHAR_JSON)   # 800 / 0.4 = 2000


def _big_result(n_rows: int = 120) -> str:
    """远超预算的工具结果（带 rows，模拟 dispatch_analysis / query_sql 的返回）。"""
    return json.dumps({
        "columns": ["region", "amount"],
        "rows": [["华东区华东区华东区", i] for i in range(n_rows)],
        "summary": {"rows_count": n_rows, "notice": "明细查询已完成"},
    }, ensure_ascii=False)


def _tool_pair(content: str) -> tuple[dict, dict]:
    """一步工具结果的协议对：assistant(tool_calls) + role=tool。"""
    return (
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "dispatch_analysis", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": content},
    )


def _tool_block(content: str) -> Block:
    return Block(kind=BlockKind.TOOL_RESULT, messages=_tool_pair(content))


# ── 单条消息：直通 / 截断 / 错误保全 ────────────────────────────────────────

def test_short_tool_content_passes_through_untouched():
    small = json.dumps({"ok": True, "canvas_action": "add_chart_block"}, ensure_ascii=False)
    out, compacted = compress_tool_message(small, BUDGET_TOKENS)
    assert out == small and compacted is False


def test_long_tool_result_is_compacted_and_rows_truncated():
    raw = _big_result()
    assert len(raw) > CHAR_BUDGET
    out, compacted = compress_tool_message(raw, BUDGET_TOKENS)

    assert compacted is True
    assert len(out) <= CHAR_BUDGET + 20          # 只留预算内（+截断标记）
    assert len(out) < len(raw)
    payload = json.loads(out)                     # 仍是合法 JSON（模型与前端都按 JSON 读）
    assert payload["rows_truncated"] is True
    assert payload["rows_total"] == 120


def test_error_results_are_never_truncated():
    """error 结果完整保留：自纠错依赖 error + hint 原文（compact_result_json 的铁律）。"""
    err = json.dumps({
        "error": "查询执行失败: Binder Error: column 'sales' not found",
        "hint": 'FROM 子句必须用: "sch"."data"；列名：' + ", ".join(f'"c{i}"' for i in range(300)),
        "available_columns": [f"c{i}" for i in range(300)],
    }, ensure_ascii=False)
    assert len(err) > CHAR_BUDGET
    out, compacted = compress_tool_message(err, BUDGET_TOKENS)
    assert out == err and compacted is False


# ── 装配层：真的接进了 for_agent_loop ───────────────────────────────────────

def test_agent_loop_compresses_tool_results_and_records_degradation():
    a = Assembler()
    raw = _big_result()
    assembled = a.for_agent_loop([_tool_block(raw)], user_msg="按地区看销售额")

    tool_msgs = [m for m in assembled.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] != raw
    assert len(tool_msgs[0]["content"]) <= CHAR_BUDGET + 20
    assert "tool_result_compacted" in assembled.ledger.degraded
    # 台账记的是**压缩后**的体积（否则"prompt 为什么这么大"仍然归因不准）
    assert 0 < assembled.ledger.entries["tool_result"] <= BUDGET_TOKENS + 50


def test_tool_protocol_pairing_is_preserved():
    """只动 content：tool_call_id 与 assistant.tool_calls 一字不改（协议要求）。"""
    a = Assembler()
    blocks = [_tool_block(_big_result())]
    assembled = a.for_agent_loop(blocks, user_msg="q")

    assistant = [m for m in assembled.messages if m.get("tool_calls")]
    tools = [m for m in assembled.messages if m.get("role") == "tool"]
    assert assistant and tools
    assert assistant[0]["tool_calls"][0]["id"] == "call_1"
    assert tools[0]["tool_call_id"] == "call_1"
    # 顺序：assistant(tool_calls) 必须紧邻其 tool 结果
    idx = assembled.messages.index(assistant[0])
    assert assembled.messages[idx + 1]["role"] == "tool"


def test_total_tool_result_budget_is_bounded_across_steps():
    """步数增长时工具结果总量线性有界：N 步 ≤ N×(预算+误差)，不再是"越滚越大"。"""
    a = Assembler()
    blocks = [_tool_block(_big_result()) for _ in range(5)]
    assembled = a.for_agent_loop(blocks, user_msg="q")

    total = assembled.ledger.entries["tool_result"]
    assert total <= 5 * (BUDGET_TOKENS + 60)
    assert total > 0


def test_compression_is_deterministic_across_steps():
    """同一条结果在第 1 步与第 3 步压出的文本必须逐字节相同——否则前缀缓存每步失效。"""
    a = Assembler()
    first_block = _tool_block(_big_result())
    early = a.for_agent_loop([first_block], user_msg="q")
    later = a.for_agent_loop(
        [first_block, _tool_block(_big_result(60)), _tool_block(_big_result(30))],
        user_msg="q",
    )

    first_early = [m for m in early.messages if m.get("role") == "tool"][0]["content"]
    first_later = [m for m in later.messages if m.get("role") == "tool"][0]["content"]
    assert first_early == first_later
