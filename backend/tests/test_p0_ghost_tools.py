# -*- coding: utf-8 -*-
"""P0-4 守卫：提示词里的工具名 / 幂等集必须**真实存在于注册表**。

修的两个确定性 bug：
1) `agent_orchestrator.py` 的纠错提示词曾教模型调 `describe_table`（注册表里没有）
   → 模型照做 → ToolExecutor 返回 fatal「未知工具」→ **整个步骤终止**（整轮白跑）。
2) 幂等集 6 个里 4 个是幽灵（list_tables / get_datasource_info / describe_table /
   analyze_column）→ 它们永远匹配不到真实工具 → **幂等静默失效**（同参数重复调用不缓存）。

这里把"提示词只能写真实工具名"和"幂等集 ⊆ 注册表"钉成永久守卫，而不只是修一次。
"""
from __future__ import annotations

import re

# 必须先 import canvas_tools 触发画布工具的延迟注册，否则注册表不完整（与
# test_architecture_consistency.py 同一前置条件）。
import app.services.canvas_tools  # noqa: F401
import app.services.agents.agent_orchestrator as AO
from app.services.agent_tools import ToolRegistry
from app.services.agents.planner_agent import _get_cached_orchestrator_tools

# 工具名形态：小写 + 下划线（本仓 20 个工具全部符合；中文散文不会误命中）
_TOOL_NAME_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

_GHOSTS = ("list_tables", "get_datasource_info", "describe_table", "analyze_column")


def _registered() -> set[str]:
    return {
        t["function"]["name"]
        for t in ToolRegistry.schemas()
        if isinstance(t, dict) and isinstance(t.get("function"), dict)
    }


def _executor_visible() -> set[str]:
    """chat 编排器 Executor 的白名单（= orchestrator_safe 去掉隐藏集）。"""
    return set(_get_cached_orchestrator_tools())


# ── 1) 纠错提示词：出现的每个工具名都必须是真实注册、且 Executor 可见 ─────────


def test_correction_prompt_tool_names_are_registered():
    """提示词里出现的**每一个**工具名 ∈ 注册表（这正是原 bug 的直接断言）。"""
    names = set(_TOOL_NAME_RE.findall(AO._NO_TOOL_CORRECTION_PROMPT))
    assert names, "纠错提示词里没有解析出工具名——提示词可能被改坏/删空"
    missing = sorted(names - _registered())
    assert not missing, f"纠错提示词教模型调不存在的工具（会 fatal 并终止整步）: {missing}"


def test_correction_prompt_tool_names_visible_to_executor():
    """只注册还不够：Executor 白名单里看不到 = 调了也会被拦。

    原 bug 的完整链条是「提示词有名字 → 白名单没有/注册表没有 → fatal」，
    所以两条都要断。
    """
    names = set(_TOOL_NAME_RE.findall(AO._NO_TOOL_CORRECTION_PROMPT))
    invisible = sorted(names - _executor_visible())
    assert not invisible, f"纠错提示词推荐的工具不在 Executor 白名单里: {invisible}"


def test_correction_prompt_declares_exactly_the_hint_constants():
    """提示词 = 由常量拼出来（防"以后又在散文里手写工具名"）。"""
    names = set(_TOOL_NAME_RE.findall(AO._NO_TOOL_CORRECTION_PROMPT))
    assert names == set(AO._CORRECTION_HINT_TOOLS)


def test_correction_prompt_keeps_guidance():
    """保留纠错引导本身（修复是"改成真实工具名"，不是"删掉这句话"）。"""
    text = AO._NO_TOOL_CORRECTION_PROMPT
    assert "步骤未完成" in text
    assert "调用合适的工具" in text
    assert "禁止只输出文字后结束步骤" in text


def test_ghost_names_are_gone():
    """4 个幽灵名不得再出现在提示词 / 幂等集里。"""
    for ghost in _GHOSTS:
        assert ghost not in AO._NO_TOOL_CORRECTION_PROMPT
        assert ghost not in AO._IDEMPOTENT_TOOLS


# ── 2) 幂等集：⊆ 注册表（否则 memo 校验对幽灵静默失效） ────────────────────


def test_idempotent_tools_are_registered():
    missing = sorted(set(AO._IDEMPOTENT_TOOLS) - _registered())
    assert not missing, f"幂等集里有不存在的工具——幂等会静默失效: {missing}"


def test_idempotent_tools_are_visible_to_executor():
    """幂等工具必须在 Executor 白名单里才有机会被调用（否则这条配置是死配置）。"""
    invisible = sorted(set(AO._IDEMPOTENT_TOOLS) - _executor_visible())
    assert not invisible, f"幂等集里的工具 Executor 看不到: {invisible}"


def test_idempotent_tools_read_only_and_meaningful():
    """防"清理完变成空集"：至少保留两个真实只读观察工具。"""
    assert {"list_datasources", "list_fields"} <= set(AO._IDEMPOTENT_TOOLS)
