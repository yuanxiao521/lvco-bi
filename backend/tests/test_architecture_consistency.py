# -*- coding: utf-8 -*-
"""架构一致性守卫：把"靠人记得同步"的约定，变成会被测试拦住的断言。

**由来**（今天连续踩的同类坑）：

1. 加了 `clear_canvas` 工具，注册进 `ToolRegistry` 也进了阶段白名单，**唯独漏了入口白名单
   `CANVAS_TOOL_NAMES`** —— 结果 LLM 物理上看不见这个工具，硬规则让它"调用 clear_canvas"
   等于让它调一个不存在的东西，它只能用看得见的 `remove_block` 凑合。三层漏斗漏了第一道，
   后面两道形同虚设。
2. 删 legacy / 删 phase / 调参数之后，**测试与旧引用没同步**，19 个测试静默失败——
   因为当时只用 `pytest --collect-only` 验证（它不执行测试体）。

这两类问题的共同点是：**不变量存在于代码里，但没有任何机制守着**。本文件把它们固化：

- 工具的四道门（注册表 → 入口白名单 → 执行层可见性 → 隐藏集）互相自洽；
- worker 注册表与派发表自洽；
- 决策 prompt 成对存在且确实分化。
"""

from __future__ import annotations

import pytest

# 注意：必须先 import canvas_tools 完成画布工具的**延迟注册**，
# 否则 ToolRegistry.schemas() 迭代时另一处注册会改字典（RuntimeError: dictionary changed size）。
import app.services.canvas_tools  # noqa: F401
from app.api.v1.ai import CANVAS_ALLOWED_TOOL_NAMES
from app.services.agent_tools import AGENT_HIDDEN_TOOLS, ToolRegistry
from app.services.agents.lead.lead_tools import _build_react_tools
from app.services.agents.lead.lead_worker import (
    ENTRY_WORKERS,
    FALLBACK_WORKER,
    WORKERS,
    resolve_worker,
)
from app.services.canvas_tools import CANVAS_TOOL_NAMES


# ── 工具可见性的四道门 ────────────────────────────────────────────────


def _registered_names() -> set[str]:
    return {t["function"]["name"] for t in ToolRegistry.schemas()}


def _entry_visible_names() -> set[str]:
    """两个入口的 react 可见工具 + 编排器白名单的并集。"""
    chat = {t["function"]["name"] for t in _build_react_tools(None)}
    canvas = {t["function"]["name"] for t in _build_react_tools(CANVAS_ALLOWED_TOOL_NAMES)}
    return chat | canvas | ToolRegistry.orchestrator_safe_tools()


def test_hidden_tools_still_registered():
    """门 1↔4：被隐藏的工具**仍在注册表**——UI 端点/前端按钮仍要能调到。

    摘除 `AGENT_HIDDEN_TOOLS` 的目的是"不给 LLM 看"，不是"删掉能力"。
    若某天顺手把实现删了，前端洞察/清洗/润色/推荐按钮会静默失效。
    """
    missing = sorted(AGENT_HIDDEN_TOOLS - _registered_names())
    assert not missing, f"这些隐藏工具已不在注册表（UI 端点会失效）: {missing}"


def test_entry_whitelists_only_reference_registered_tools():
    """门 1↔2：入口白名单不得引用未注册的工具（幽灵引用）。"""
    registered = _registered_names()
    ghosts = sorted((CANVAS_TOOL_NAMES | CANVAS_ALLOWED_TOOL_NAMES | _entry_visible_names()) - registered)
    assert not ghosts, f"白名单引用了未注册的工具: {ghosts}"


def test_every_visible_tool_is_reachable_from_some_entry():
    """门 2 完整性：每个"对 agent 可见"的工具，至少要在一个入口里可达。

    这是 **clear_canvas 那个坑的直接守卫**：注册了、也进了某层白名单，但入口白名单漏了它，
    于是"存在却永远选不到"。任何新增工具忘记挂入口，这里会立刻失败。
    """
    visible = _registered_names() - AGENT_HIDDEN_TOOLS
    unreachable = sorted(visible - _entry_visible_names())
    assert not unreachable, f"这些工具对 agent 可见，但没有任何入口能用到: {unreachable}"


def test_entry_whitelists_exclude_hidden_tools():
    """门 3：隐藏工具不得出现在任何入口的 LLM 可见 schema 里。"""
    leaked = sorted(AGENT_HIDDEN_TOOLS & _entry_visible_names())
    assert not leaked, f"隐藏工具泄漏进了 LLM 可见清单: {leaked}"


def test_orchestrator_whitelist_excludes_hidden_tools():
    """门 3：编排器白名单同样过滤隐藏集。"""
    leaked = sorted(AGENT_HIDDEN_TOOLS & ToolRegistry.orchestrator_safe_tools())
    assert not leaked, f"隐藏工具泄漏进编排器白名单: {leaked}"


def test_canvas_write_tools_not_visible_in_chat_entry():
    """入口隔离：画布写入类工具不应出现在对话入口（对话页没有画布）。"""
    chat = {t["function"]["name"] for t in _build_react_tools(None)}
    leaked = sorted(CANVAS_TOOL_NAMES & chat)
    assert not leaked, f"画布写入工具泄漏进对话入口: {leaked}"


def test_canvas_write_tools_visible_in_canvas_entry():
    """入口完整性：**画布写工具**必须在画布入口的 react 路径可见。

    ⚠️ 关键：这里不能用 `CANVAS_TOOL_NAMES` 当"画布写工具"的判据——那是**循环定义**
    （把某工具从 `CANVAS_TOOL_NAMES` 删掉，断言就 trivial 通过，正是本测试要拦的 bug）。
    权威来源取**下游**：编排器执行器白名单 `_EXECUTOR_TOOL_NAMES`（去掉仅供列名自纠错的
    `list_fields`）——它代表"编排器真的会去执行的画布工具"。

    回归背景：`clear_canvas` 曾只注册进 ToolRegistry 与阶段白名单、**漏了入口白名单**，
    于是画布走 react 路径（canvas + simple）时 LLM 物理上看不见它，只能用 `remove_block`
    逐块凑合；`get_canvas_layout` 也有过同类漏挂（被"阶段外工具"拦下 → 白跑一轮）。
    """
    from app.services.agents.canvas_orchestrator import _EXECUTOR_TOOL_NAMES

    canvas_entry = {t["function"]["name"] for t in _build_react_tools(CANVAS_ALLOWED_TOOL_NAMES)}
    write_tools = set(_EXECUTOR_TOOL_NAMES) - {"list_fields"}
    missing = sorted(write_tools - canvas_entry)
    assert not missing, f"画布写工具在画布入口的 react 路径不可见: {missing}"


def test_executor_tools_stay_within_canvas_entry_whitelist():
    """下游 ⊆ 入口：编排器执行器白名单引用的工具，必须都在画布入口白名单里。

    这是"三层漏斗漏了第一道"的通用守卫——入口白名单（LLM 可见）是漏斗第一道，
    下游（Planner / Executor）引用的任何工具都必须先过它，否则就是"注册了、也能规划，
    但 LLM 根本看不见"，整条链路空转。
    """
    from app.services.agents.canvas_orchestrator import _EXECUTOR_TOOL_NAMES

    canvas_entry = {t["function"]["name"] for t in _build_react_tools(CANVAS_ALLOWED_TOOL_NAMES)}
    leaked = sorted(set(_EXECUTOR_TOOL_NAMES) - canvas_entry)
    assert not leaked, f"编排器执行器引用了画布入口白名单之外的工具: {leaked}"


# ── worker 注册表自洽 ────────────────────────────────────────────────


def test_entry_workers_reference_registered_workers():
    """派发表引用的 worker 名必须都在注册表里（防"派到一个不存在的内核"）。"""
    referenced = {name for table in ENTRY_WORKERS.values() for name in table.values()}
    unknown = sorted(referenced - set(WORKERS))
    assert not unknown, f"ENTRY_WORKERS 引用了未注册的 worker: {unknown}"


def test_worker_key_matches_mode():
    """worker 的 key 与其 `mode` 一致——单一词汇表，避免"worker 名"与"mode 名"两套命名互译。"""
    mismatched = {k: v.mode for k, v in WORKERS.items() if k != v.mode}
    assert not mismatched, f"worker key 与 mode 不一致: {mismatched}"


def test_fallback_worker_registered():
    """兜底 worker 必须存在，否则未登记入口会派发失败。"""
    assert FALLBACK_WORKER in WORKERS, f"兜底 worker {FALLBACK_WORKER!r} 不在注册表"


def test_every_entry_has_fallback_path():
    """每个登记入口的每种复杂度都要能解析出 worker（缺格会落兜底，不该抛错）。"""
    for entry in ENTRY_WORKERS:
        for complexity in ("simple", "complex", ""):
            spec = resolve_worker(entry, complexity)
            assert spec.mode in WORKERS, f"{entry}/{complexity!r} 解析出未知 worker"


def test_unknown_entry_falls_back_without_raising():
    """未登记入口 → 兜底通用编排（不抛错：派发在主链路上，宁可退化也别整轮失败）。"""
    assert resolve_worker("nonexistent_entry", "complex").mode == WORKERS[FALLBACK_WORKER].mode


# ── 决策 prompt 成对分化 ─────────────────────────────────────────────


def test_both_decision_prompts_loaded_and_distinct():
    """两个入口的决策 prompt 都必须能加载、非空、且**内容不同**。

    此前两个入口共用一份 prompt，模型无从知道自己在哪个页面（画布 vs 对话）；
    分化后若有人只改一份、或把回落逻辑接错，这里会拦住。
    """
    from app.services.ai_prompts import LEAD_MERGED_SYSTEM, LEAD_MERGED_SYSTEM_CANVAS

    assert LEAD_MERGED_SYSTEM.strip(), "对话版决策 prompt 为空"
    assert LEAD_MERGED_SYSTEM_CANVAS.strip(), "画布版决策 prompt 为空"
    assert LEAD_MERGED_SYSTEM != LEAD_MERGED_SYSTEM_CANVAS, "两份决策 prompt 内容相同（分化被回退）"


def test_decision_prompts_declare_their_scene():
    """两份 prompt 各自声明所处场景——这是"决策大脑知道自己在哪"的最小证据。"""
    from app.services.ai_prompts import LEAD_MERGED_SYSTEM, LEAD_MERGED_SYSTEM_CANVAS

    assert "对话页" in LEAD_MERGED_SYSTEM, "对话版 prompt 未声明场景"
    assert "分析画布" in LEAD_MERGED_SYSTEM_CANVAS, "画布版 prompt 未声明场景"


@pytest.mark.parametrize("prompt_name", ["lead_merged_system", "lead_merged_system_canvas"])
def test_decision_prompt_registry_loadable(prompt_name: str):
    """两份 prompt 都能从 PromptRegistry 取到（防止 YAML 改名/损坏导致静默回落）。"""
    from app.services.prompt_registry import get_prompt  # noqa: PLC0415

    tmpl = get_prompt(prompt_name)
    assert tmpl and tmpl.system and tmpl.system.strip(), f"{prompt_name} 从注册表加载为空"
