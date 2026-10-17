# -*- coding: utf-8 -*-
"""P1 工具层：契约、硬边界与统一 prompt。

任务书 §6 不变量 1：**写工具永不进 LLM 调用面**——"注册表里就不存在"，
而不是"注册了再拦截"。本文件把这个边界钉死，并且让清单**自动对齐**
canvas_tools.py 的真实声明（将来新增写工具，这里会立刻变红）。
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import yaml

from app.config import Settings
from app.services.agents.lead.lead_agent_tools import (
    DISPATCH_TOOL_NAMES,
    OBSERVE_TOOL_NAMES,
    QUOTA,
    TERMINAL_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    decision_action,
    lead_tool_specs,
    registered_tool_names,
    tool_class,
)

BACKEND = Path(__file__).resolve().parents[1]


# ── 硬边界 ─────────────────────────────────────────────────────────────────

def test_registry_contains_no_write_tools():
    """§6 不变量 1：注册表 ∩ 写工具 = ∅。"""
    overlap = registered_tool_names() & WRITE_TOOL_NAMES
    assert not overlap, f"写工具混进了 Lead 工具表：{sorted(overlap)}"


def test_write_tool_names_match_canvas_tools_declarations():
    """写工具清单必须与 canvas_tools.py 的真实声明逐名对齐（自动发现，防止漏网）。

    如果将来 canvas_tools 新增了一个写工具而 WRITE_TOOL_NAMES 没跟上，
    上面那条守卫就会**静默失效**（新写工具不在清单里 → 交集检查查不到它）。
    这条用例专门堵这个洞。
    """
    src = (BACKEND / "app/services/canvas_tools.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'^\s*name = "(add_\w+|update_\w+|remove_\w+|clear_\w+)"',
                              src, re.MULTILINE))
    assert declared, "没扫到 canvas_tools 的写工具声明，正则可能失效（守卫空转）"
    missing = declared - WRITE_TOOL_NAMES
    assert not missing, f"canvas_tools 有这些写工具未登记到 WRITE_TOOL_NAMES：{sorted(missing)}"


# ── 工具表结构 ─────────────────────────────────────────────────────────────

def test_registry_partition_is_exact_and_disjoint():
    """注册表必须恰好是三类之和，且三类互不相交。"""
    assert OBSERVE_TOOL_NAMES & DISPATCH_TOOL_NAMES == frozenset()
    assert OBSERVE_TOOL_NAMES & TERMINAL_TOOL_NAMES == frozenset()
    assert DISPATCH_TOOL_NAMES & TERMINAL_TOOL_NAMES == frozenset()
    expected = OBSERVE_TOOL_NAMES | DISPATCH_TOOL_NAMES | TERMINAL_TOOL_NAMES
    assert registered_tool_names() == set(expected)


def test_terminal_actions_are_exactly_three():
    """§2.3：必须以 answer / ask_user / stop 之一收尾。"""
    assert TERMINAL_TOOL_NAMES == frozenset({"answer", "ask_user", "stop"})
    for name in TERMINAL_TOOL_NAMES:
        assert tool_class(name) == "terminal"


def test_every_spec_is_well_formed_and_classifiable():
    specs = lead_tool_specs()
    assert len(specs) == len(registered_tool_names()), "工具名重复"
    for s in specs:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["description"], f"工具 {fn.get('name')} 缺名称/描述"
        params = fn["parameters"]
        assert params["type"] == "object"
        for req in params.get("required", []):
            assert req in params["properties"], f"{fn['name']} 的 required 字段 {req} 未声明"
        assert tool_class(fn["name"]) is not None, f"{fn['name']} 不属于任何类别"


def test_quota_covers_observe_dispatch_and_independent_repair():
    """配额覆盖观察 / 派发，外加 P2.5 的 **repair 独立上限**（§6 不变量 2b）；
    终止动作不占配额（它结束循环）。

    为什么 repair 要独立：否则"补一下 → 仍不达标 → 再补"会把派发额度吃光，
    还会掩盖"方向本身就错了"这个更该被发现的事实。
    """
    assert set(QUOTA) == {"observe", "dispatch", "repair"}
    assert QUOTA["observe"] == 2 and QUOTA["dispatch"] == 3 and QUOTA["repair"] == 2


# ── 统一 prompt ────────────────────────────────────────────────────────────

def test_unified_prompt_declares_ask_kind():
    """统一 prompt 必须声明 ask_kind 与两种取值。

    这是既有守卫（test_prompt_config_consistency）踩过的坑：提示词不声明该字段
    → 模型不输出 → 解析端永远拿默认 clarify → **前端确认卡片永不出现**。
    统一 prompt 是新的决策入口，必须一起守住。
    """
    from app.services.ai_prompts import LEAD_AGENT_SYSTEM

    for token in ("ask_kind", "confirm", "clarify"):
        assert token in LEAD_AGENT_SYSTEM, f"LEAD_AGENT_SYSTEM 未声明 {token}"


def test_unified_prompt_loads_from_yaml_not_fallback():
    """生效的统一 prompt 必须来自 yaml（否则改 yaml 不会生效且无人发现）。"""
    from app.services.ai_prompts import LEAD_AGENT_SYSTEM

    data = yaml.safe_load(
        (BACKEND / "prompts" / "lead_agent_system.yaml").read_text(encoding="utf-8"))
    yaml_system = textwrap.dedent(data["system"]).strip("\n")
    assert yaml_system == LEAD_AGENT_SYSTEM, (
        "生效的 lead_agent_system 与 prompts/lead_agent_system.yaml 不一致——"
        "yaml 可能加载失败，正在跑 Python fallback"
    )


def test_unified_prompt_states_the_write_boundary():
    """prompt 必须明确告诉模型"写工具不可见"，与代码层的硬边界呼应。"""
    from app.services.ai_prompts import LEAD_AGENT_SYSTEM

    assert "写工具" in LEAD_AGENT_SYSTEM
    assert "add_chart_block" in LEAD_AGENT_SYSTEM  # 点名，避免模型凭直觉尝试


# ── 决策理由（method a）：reason 字段 + 动作枚举映射 ──────────────────────

def test_every_tool_declares_reason_field():
    """每个工具都必须带 reason（决策理由）。

    method a：reason 由**模型自述**，SSE decision 事件用它显示"为什么这么判断"。
    统一在 _fn 里注入，所以新增工具漏不掉——这条测试防止有人绕过 _fn 手写 spec。
    """
    for spec in lead_tool_specs():
        name = spec["function"]["name"]
        props = spec["function"]["parameters"]["properties"]
        assert "reason" in props, f"{name} 缺 reason 字段"
        assert props["reason"]["type"] == "string"


def test_decision_action_maps_to_legacy_enum():
    """decision.action 只能落在 legacy 的 4 个枚举值上，避免前端出现两套词汇。"""
    assert decision_action("answer") == "answer"
    assert decision_action("ask_user") == "ask_user"
    assert decision_action("stop") == "stop"
    assert decision_action("dispatch_analysis") == "call_analysis"
    assert decision_action("repair") == "call_analysis"   # repair 也是"去干活"


# ── 开关（可回滚）──────────────────────────────────────────────────────────

def test_mode_defaults_to_legacy_so_p1_is_opt_in():
    """默认必须是 legacy：P1 要能一键回滚，双跑对拍通过后才切默认。"""
    assert Settings.model_fields["LEAD_AGENT_MODE"].default == "legacy"
    assert Settings.model_fields["LEAD_AGENT_MAX_STEPS"].default == 6
