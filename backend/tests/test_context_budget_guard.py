"""P1-A 上下文防炸：超窗守卫 + 指标/窗口有界 + 前缀稳定（块顺序）。

对应 task-12 四条验收：
1. 超窗装配 → 按优先级裁剪，messages 总量 ≤ 上限（且 `trimmed=True`、台账留痕）
2. 未超窗 → 逐字节不变（守卫是 no-op）
3. 指标超 30 条 → 被截断且带"用 list_metrics 取全量"提示
4. 块顺序 → canvas 在最后（派发后易变块后置，前缀稳定）

测试策略：
- **算法层**用 monkeypatch 把预算压到几百 token，这样裁剪顺序可以精确断言、跑得快；
- **配置层**另有一个用真实 64000×0.75 预算的用例，证明默认配置下真的会裁；
- 一切都是纯内存对象，不连 DB / 不调 LLM。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.context import assembler as asm
from app.services.context.assembler import (
    TRIM_HARD_CAP,
    TRIM_SUBTASK,
    TRIM_TOOL_RESULT,
    TRIM_WINDOW,
    OVER_BUDGET_UNSATISFIABLE,
    Assembler,
    input_token_budget,
    messages_tokens,
)
from app.services.context.provider import (
    TurnContextProvider,
    TurnInputs,
    ordered_agent_loop_blocks,
)
from app.services.context.types import Block, BlockKind, Priority
from app.services.metric_service import (
    DEFAULT_METRICS_MAX_CHARS,
    DEFAULT_METRICS_MAX_ITEMS,
    format_metrics_context,
)


# ── 工具函数 ────────────────────────────────────────────────────────────────


def _txt(n_chars: int, ch: str = "数") -> str:
    """构造 n_chars 个 CJK 字符（≈ n_chars token，便于精确控制体积）。"""
    return ch * n_chars


def _system_block(text: str = "SYS") -> Block:
    return Block(kind=BlockKind.SYSTEM, text=text, priority=Priority.SYSTEM, compressible=False)


def _window_block(messages: list[dict]) -> Block:
    return Block(
        kind=BlockKind.WINDOW, messages=tuple(messages),
        priority=Priority.WINDOW, compressible=True,
    )


def _subtask_block(text: str) -> Block:
    return Block(kind=BlockKind.SUBTASK, text=text, priority=Priority.SUBTASK, compressible=True)


def _tool_block(call_id: str, payload: str) -> Block:
    """一组工具结果：assistant(tool_calls) + role=tool（协议配对必须整组同生共死）。"""
    return Block(
        kind=BlockKind.TOOL_RESULT,
        messages=(
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "dispatch_analysis", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": payload},
        ),
        priority=Priority.TOOL_RESULT,
        compressible=False,
    )


def _provider(**kw) -> TurnContextProvider:
    inputs = dict(
        user_id="u1", session_id="s1", entry="canvas",
        turns=[{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}],
        history_summary="上轮聊了销售额",
        subtask_summaries=["[子任务执行记录] 已生成 3 块", "[子任务评估] 上轮达标"],
        datasources=[{"id": 7, "name": "DS", "type": "duckdb"}],
        metrics_ctx="【销售额】= SUM(amount)",
        canvas_layout="画布：1 个图表块",
        artifact_note="上轮已生成柱状图",
    )
    inputs.update(kw)
    return TurnContextProvider.for_turn(TurnInputs(**inputs))


def _assert_under_budget(assembled) -> None:
    total = messages_tokens(assembled.messages)
    budget = input_token_budget()
    assert total <= budget, f"仍超窗：{total} > {budget}（守卫失效）"


# ═══════════════════════════════════════════════════════════════════════════
# 0. 预算口径
# ═══════════════════════════════════════════════════════════════════════════


def test_budget_is_window_minus_output_reserve():
    """budget = LLM_CONTEXT_WINDOW_TOKENS × (1 − RESERVE_RATIO) = 64000 × 0.75。"""
    assert input_token_budget() == int(settings.LLM_CONTEXT_WINDOW_TOKENS * 0.75)
    assert input_token_budget() == 48000


def test_budget_is_defensive_against_bad_config(monkeypatch):
    """离谱配置不能把预算算成 0/负数——那会把整轮上下文裁空。"""
    monkeypatch.setattr(settings, "LLM_CONTEXT_WINDOW_TOKENS", 0, raising=False)
    assert input_token_budget() > 0
    monkeypatch.setattr(settings, "LLM_CONTEXT_WINDOW_TOKENS", 10000, raising=False)
    monkeypatch.setattr(settings, "LLM_CONTEXT_OUTPUT_RESERVE_RATIO", 0.99, raising=False)
    assert input_token_budget() == 1000  # 比例被夹到 0.9 → 只剩 10% 给输入


# ═══════════════════════════════════════════════════════════════════════════
# ① 超窗 → 裁剪，且总量 ≤ 上限
# ═══════════════════════════════════════════════════════════════════════════


def test_over_budget_provider_assembly_gets_trimmed_under_real_budget():
    """真实配置（48000）下：大窗口 + 多组工具结果 → 必须裁剪到预算以内。"""
    big_turns = []
    for i in range(12):
        big_turns.append({"role": "user", "content": _txt(1500)})
        big_turns.append({"role": "assistant", "content": _txt(1500)})
    p = _provider(turns=big_turns)
    for i in range(8):
        p.add_tool_result([
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": f"c{i}", "type": "function",
                             "function": {"name": "dispatch_analysis", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": _txt(2000)},
        ])

    a = p.for_agent_loop("按地区看销售额", system_text="SYS")

    assert messages_tokens(a.messages) > 0
    _assert_under_budget(a)
    assert a.trimmed is True
    assert TRIM_TOOL_RESULT in a.ledger.degraded
    # 最早的工具组被整组丢掉（assistant.tool_calls 与其 tool 结果一起消失）
    ids = [m.get("tool_call_id") for m in a.messages if m.get("role") == "tool"]
    assert "c0" not in ids
    # 最近的一组还在（裁剪从最旧开始，不是一刀切）
    assert ids, "不能把工具结果全部丢光"


def test_trim_order_is_tool_then_window_then_subtask(monkeypatch):
    """优先级：先丢工具结果 → 再丢窗口 → 最后才动子任务摘要。"""
    monkeypatch.setattr(asm, "input_token_budget", lambda: 600)
    a = Assembler()
    blocks = [
        _system_block("SYS"),
        _window_block([
            {"role": "user", "content": _txt(200)},
            {"role": "assistant", "content": _txt(200)},
            {"role": "user", "content": _txt(200)},
        ]),
        _subtask_block("- 旧摘要" + _txt(100) + "\n- 新摘要" + _txt(100)),
        _tool_block("c1", _txt(300)),
        _tool_block("c2", _txt(300)),
    ]
    out = a.for_agent_loop(blocks, user_msg="问题")

    assert out.trimmed is True
    assert TRIM_TOOL_RESULT in out.ledger.degraded
    # 顺序：工具结果的降级必须早于窗口的降级（列表按首次出现顺序）
    if TRIM_WINDOW in out.ledger.degraded:
        assert out.ledger.degraded.index(TRIM_TOOL_RESULT) < out.ledger.degraded.index(TRIM_WINDOW)
    _assert_under_budget(out)


def test_window_trimming_keeps_newest_messages(monkeypatch):
    """窗口裁剪从最旧开始：最新一轮必须还在。"""
    monkeypatch.setattr(asm, "input_token_budget", lambda: 300)
    a = Assembler()
    turns = [{"role": "user", "content": f"旧问题{i}" + _txt(120)} for i in range(6)]
    turns.append({"role": "user", "content": "最新问题"})
    out = a.for_agent_loop(
        [_system_block("SYS"), _window_block(turns)], user_msg="当前问题"
    )
    assert TRIM_WINDOW in out.ledger.degraded
    contents = [m["content"] for m in out.messages if m.get("role") == "user"]
    assert "最新问题" in contents, "裁剪不得把最新一轮对话丢掉"
    _assert_under_budget(out)


def test_subtask_oldest_entries_are_dropped_and_parts_updated(monkeypatch):
    """子任务摘要按"最旧条目优先"丢，且 parts 与 messages 保持一致。"""
    monkeypatch.setattr(asm, "input_token_budget", lambda: 160)
    a = Assembler()
    subtask = "- 最旧条目" + _txt(60) + "\n- 中间条目" + _txt(60) + "\n- 最新条目" + _txt(60)
    out = a.for_agent_loop(
        [_system_block("S"), _subtask_block(subtask)], user_msg="q"
    )
    assert TRIM_SUBTASK in out.ledger.degraded
    system = out.messages[0]["content"]
    assert "最新条目" in system, "最新一条评估必须保留"
    assert "最旧条目" not in system, "最旧条目应被先丢"
    assert "最旧条目" not in out.parts[BlockKind.SUBTASK.value]
    _assert_under_budget(out)


def test_never_silently_over_budget_even_if_untrimmable(monkeypatch):
    """system 提示词自身就超预算 → 裁不动，但**必须留痕**（不许静默超窗）。"""
    monkeypatch.setattr(asm, "input_token_budget", lambda: 50)
    a = Assembler()
    out = a.for_agent_loop([_system_block(_txt(500))], user_msg="q")
    assert out.trimmed is True
    assert OVER_BUDGET_UNSATISFIABLE in out.ledger.degraded


def test_hard_cap_truncates_largest_block_when_structure_exhausted(monkeypatch):
    """结构性块（工具/窗口/子任务）都丢完仍超 → 兜底截断文本块。"""
    monkeypatch.setattr(asm, "input_token_budget", lambda: 120)
    a = Assembler()
    metrics = Block(kind=BlockKind.METRICS, text=_txt(800))
    out = a.for_agent_loop([_system_block("S"), metrics], user_msg="q")
    assert TRIM_HARD_CAP in out.ledger.degraded
    _assert_under_budget(out)


# ═══════════════════════════════════════════════════════════════════════════
# ② 未超窗 → 逐字节不变（守卫是 no-op）
# ═══════════════════════════════════════════════════════════════════════════


def test_under_budget_is_byte_identical_to_naive_rendering():
    """未超窗时，守卫不得改变任何一个字节：messages 必须等于"按块顺序直接拼接"。"""
    p = _provider()
    p.add_tool_result([
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "dispatch_analysis", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "第 1 步完成。"},
    ])
    a = p.for_agent_loop("那华东呢", system_text="SYS")

    assert a.trimmed is False
    assert not [d for d in a.ledger.degraded if d.startswith("context_trimmed")]

    # 独立重建期望值（不复用被测代码的渲染函数）
    expected_ctx = (
        "【历史记忆】\n上轮聊了销售额\n\n"
        "【可用数据源】\n- id=7 name=DS type=duckdb\n\n"
        "【受治理指标清单】\n【销售额】= SUM(amount)\n\n"
        "【已完成子任务摘要】\n- [子任务执行记录] 已生成 3 块\n- [子任务评估] 上轮达标\n\n"
        "【最近一次子任务评估】\n[子任务评估] 上轮达标\n\n"
        "【当前画布布局（已落盘，以此为准）】\n画布：1 个图表块\n\n"
        "【上一轮已产出内容】\n上轮已生成柱状图"
    )
    assert a.messages[0] == {"role": "system", "content": "SYS\n\n" + expected_ctx}
    assert a.messages[1] == {"role": "user", "content": "你好"}
    assert a.messages[2] == {"role": "assistant", "content": "在"}
    assert a.messages[3] == {"role": "user", "content": "那华东呢"}
    assert a.messages[-2]["tool_calls"][0]["id"] == "c1"
    assert a.messages[-1]["tool_call_id"] == "c1"
    _assert_under_budget(a)


def test_guard_is_deterministic_across_rebuilds():
    """同一输入重复装配必须逐字节相同（前缀缓存/评测可比性的前提）。"""
    p = _provider()
    a = p.for_agent_loop("x", system_text="SYS")
    b = p.for_agent_loop("x", system_text="SYS")
    assert a.messages == b.messages
    assert a.trimmed is False and b.trimmed is False


# ═══════════════════════════════════════════════════════════════════════════
# ③ 指标有界化
# ═══════════════════════════════════════════════════════════════════════════


def _metric(i: int, *, name: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        key=f"m{i}",
        name=name or "指标",
        description=None,
        formula="SUM(x)",
    )


def test_metrics_truncated_to_30_items_with_hint():
    """120 条指标 → 最多 30 条 + 明确提示"用 list_metrics 取全量"。"""
    metrics = [_metric(i) for i in range(120)]
    ctx = format_metrics_context(metrics)

    assert ctx.count("- key=") == DEFAULT_METRICS_MAX_ITEMS == 30
    assert len(ctx) <= DEFAULT_METRICS_MAX_CHARS
    assert "共 120 个可用指标" in ctx
    assert "list_metrics" in ctx
    # 截断时不能把"还有全量"这件事藏起来，否则模型会臆造 key
    assert "仅展示前 30 个示例" in ctx


def test_metrics_char_cap_wins_when_lines_are_long():
    """单条很长时，字符上限先生效（仍必须 ≤ max_chars 且有提示）。"""
    metrics = [_metric(i, name="很长的指标名称" * 10) for i in range(40)]
    ctx = format_metrics_context(metrics)
    assert len(ctx) <= DEFAULT_METRICS_MAX_CHARS
    assert ctx.count("- key=") < DEFAULT_METRICS_MAX_ITEMS
    assert "list_metrics" in ctx


def test_metrics_untouched_when_under_limits():
    """未超限 → 输出与旧实现逐字节一致（老测试/评测基线不受影响）。"""
    m = _metric(1, name="销售额")
    m.key = "sales"
    m.description = "成交金额"
    m.formula = "SUM({{amount}})"
    expected = (
        "可引用指标清单：\n"
        "可用指标（建议优先用指标 key 而非裸字段聚合）：\n"
        "- key=sales｜销售额，成交金额（表达式: SUM({{amount}})）"
    )
    assert format_metrics_context([m]) == expected
    # 空清单仍然是空串（调用方据此不注入该块）
    assert format_metrics_context([]) == ""


def test_metrics_caps_are_overridable():
    metrics = [_metric(i) for i in range(50)]
    ctx = format_metrics_context(metrics, max_items=5, max_chars=400)
    assert ctx.count("- key=") == 5
    assert len(ctx) <= 400


# ═══════════════════════════════════════════════════════════════════════════
# ④ 块顺序：恒定块在前、易变块在最后（前缀稳定）
# ═══════════════════════════════════════════════════════════════════════════


def test_block_order_is_stable_blocks_first_canvas_last():
    a = _provider().for_agent_loop("x", system_text="SYS")
    system = a.messages[0]["content"]

    order = [
        "【历史记忆】",
        "【可用数据源】",
        "【受治理指标清单】",
        "【已完成子任务摘要】",
        "【最近一次子任务评估】",
        "【当前画布布局（已落盘，以此为准）】",
        "【上一轮已产出内容】",
    ]
    positions = [system.index(token) for token in order]
    assert positions == sorted(positions), f"块顺序不符合恒定在前：{positions}"

    # canvas 是**最大的可变块**，必须排在 metrics/subtask/assessment 之后；
    # 唯一排在它后面的是 artifact（请求级常量、体积很小）。
    i_canvas = system.index("【当前画布布局（已落盘，以此为准）】")
    for volatile in ("【受治理指标清单】", "【已完成子任务摘要】", "【最近一次子任务评估】"):
        assert i_canvas > system.index(volatile)


def test_ordered_blocks_helper_preserves_relative_order_within_kind():
    blocks = [
        _tool_block("c1", "x"),
        _subtask_block("- a"),
        _system_block("SYS"),
        _subtask_block("- b"),
        _window_block([]),
    ]
    ordered = ordered_agent_loop_blocks(blocks)
    kinds = [b.kind for b in ordered]
    assert kinds == [
        BlockKind.SYSTEM, BlockKind.SUBTASK, BlockKind.SUBTASK,
        BlockKind.WINDOW, BlockKind.TOOL_RESULT,
    ]
    # 同类块保持原有相对顺序（stability），不因排序被打乱
    assert [b.text for b in ordered if b.kind is BlockKind.SUBTASK] == ["- a", "- b"]


def test_canvas_refresh_only_invalidates_tail_of_system():
    """前缀稳定的闭环：换画布后，system 的前缀（恒定块）必须一字不变。"""
    p = _provider()
    before = p.for_agent_loop("x", system_text="SYS").messages[0]["content"]
    p.refresh_canvas("画布：2 个图表块（新增直营占比）")
    after = p.for_agent_loop("x", system_text="SYS").messages[0]["content"]

    assert before != after
    # 共同前缀至少覆盖到"指标清单"结束（恒定块全部还在前缀里）
    marker = "【受治理指标清单】"
    head_before = before[: before.index(marker)]
    assert after.startswith(head_before), "恒定块必须仍在前缀里，否则缓存全废"


# ═══════════════════════════════════════════════════════════════════════════
# window 块可压缩：压缩策略真的可达
# ═══════════════════════════════════════════════════════════════════════════


def test_window_block_is_compressible():
    from app.services.context.sources.window import WindowSource

    block = WindowSource([{"role": "user", "content": "hi"}], 30).load()
    assert block.compressible is True, "WINDOW 必须可压缩，否则 MessageListPolicy 永远不可达"


def test_window_over_keep_limit_is_folded_by_message_list_policy():
    """> CONTEXT_KEEP(60) 条时，compress.MessageListPolicy 真的被接上（折叠出 digest）。"""
    turns = []
    for i in range(70):
        turns.append({"role": "user", "content": f"问题{i}"})
        turns.append({"role": "assistant", "content": f"回答{i}"})
    a = Assembler()
    out = a.for_agent_loop([_system_block("S"), _window_block(turns)], user_msg="q")

    assert "message_list_folded" in out.ledger.degraded
    window_msgs = [m for m in out.messages if m.get("role") in ("user", "assistant")][:-1]
    assert len(window_msgs) < len(turns)
    assert any("已省略" in str(m.get("content")) for m in window_msgs)
