# -*- coding: utf-8 -*-
"""P4 装配统一：TurnContextProvider.for_agent_loop 是**唯一**的循环装配出口。

任务书 §5：把「按调用点分三出口」演进为「按循环阶段装配」——
  · 出口收敛：for_decision / for_answer / for_worker 降级为内部实现，
    对外暴露 for_agent_loop(step)；
  · 新增块类型 TOOL_RESULT（观察/评审/派发的返回）；
  · 循环感知：step0 全量、后续步只追加工具结果增量；
  · 台账：所有块（含工具结果、含原来手拼的四块）统一进 Ledger。
"""
from __future__ import annotations

from app.services.context.budgets import active_table
from app.services.context.provider import TurnContextProvider, TurnInputs
from app.services.context.types import BlockKind


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


def _tool_msgs() -> list[dict]:
    return [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "dispatch_analysis", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "已派发子任务，评审通过。"},
    ]


# ── step0：全量 ────────────────────────────────────────────────────────────

def test_step0_injects_every_context_block_and_records_ledger():
    a = _provider().for_agent_loop("那华东呢", step=0, system_text="SYS")
    system = a.messages[0]["content"]

    assert system.startswith("SYS")
    # 共用 sources 的渲染函数（同一事实只有一处渲染）
    for token in ("历史记忆", "受治理指标清单", "可用数据源", "id=7", "当前画布布局",
                  "已完成子任务摘要", "最近一次子任务评估"):
        assert token in system, f"上下文缺少 {token}"

    # 窗口与用户消息是**真消息**，不塞进 system
    assert {"role": "user", "content": "你好"} in a.messages
    assert a.messages[-1] == {"role": "user", "content": "那华东呢"}

    # §5 台账：所有块都要进 Ledger
    assert a.ledger.purpose == "agent_loop"
    for name in ("system", "memory", "window", "datasources", "canvas", "metrics"):
        assert name in a.ledger.entries, f"台账缺 {name}"


def test_window_is_real_messages_not_system_text():
    a = _provider().for_agent_loop("x", step=0, system_text="SYS")
    roles = [m["role"] for m in a.messages]
    assert roles[0] == "system"
    assert "user" in roles[1:] and "assistant" in roles[1:]


# ── step>0：只追加增量 ─────────────────────────────────────────────────────

def test_step_gt0_does_not_reinject_state_blocks():
    """后续步不重复注入状态块——它们已在首步 system 里；重复注入白烧 token，
    还会让模型看到两份互相矛盾的画布快照。"""
    a = _provider().for_agent_loop("x", step=3, system_text="SYS")
    system = a.messages[0]["content"]
    assert system == "SYS", "step>0 只该保留 system 本体"
    for token in ("历史记忆", "可用数据源", "当前画布布局"):
        assert token not in system


def test_tool_result_increment_is_appended_in_order():
    p = _provider()
    p.add_tool_result(_tool_msgs())
    a = p.for_agent_loop("x", step=1, system_text="SYS")

    tool_msgs = [m for m in a.messages if m.get("role") in ("assistant", "tool")
                 and m.get("tool_call_id") or m.get("tool_calls")]
    assert len(tool_msgs) == 2, "工具结果增量必须带进来"
    # tool_call_id 与顺序原样保留（OpenAI 工具协议要求，压缩不得破坏）
    assert tool_msgs[0]["tool_calls"][0]["id"] == "c1"
    assert tool_msgs[1]["tool_call_id"] == "c1"
    assert a.messages[-1] == {"role": "user", "content": "x"}


def test_tool_result_recorded_in_ledger():
    p = _provider()
    p.add_tool_result(_tool_msgs())
    a = p.for_agent_loop("x", step=1, system_text="SYS")
    assert a.ledger.entries.get(BlockKind.TOOL_RESULT.value, 0) >= 0


# ── 预算表 ─────────────────────────────────────────────────────────────────

def test_agent_loop_budget_table_exists_and_is_not_enforced_yet():
    """agent_loop 表必须存在且**暂不强制压缩**：本阶段只做「统一出口 + 台账覆盖」，
    改 enforced 会直接改 prompt 内容，必须单独一轮调优 + 配评测。"""
    table = active_table("agent_loop")
    assert table.purpose == "agent_loop"
    assert table.enforced is False
    names = {b.kind for b in table.blocks}
    assert {"system", "tool_schema", "tool_result", "memory", "window"} <= names
