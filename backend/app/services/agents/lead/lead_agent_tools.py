# -*- coding: utf-8 -*-
"""Lead 的工具层（P1）：把「能力」声明成工具契约，并守住硬边界。

三类工具（对齐任务书 §2.2）：
  观察（只读）  get_canvas_layout / list_datasources / list_fields / list_metrics / query_sample
  执行（代码）  dispatch_analysis / repair
  终止（动作）  answer / ask_user / stop

**硬边界（§6 不变量 1）**：写工具**永不注册**——注册表里就不存在，
不是"注册了再拦截"。WRITE_TOOL_NAMES 与 services/canvas_tools.py 的真实声明对齐，
由测试断言"注册表 ∩ 写工具 = ∅"，防止将来有人图省事把写工具塞进来。

**终止动作也是"工具"**（模型通过调用它们收尾），但循环对它们做终止处理：
先于步数闸生效；一步都没产出终止动作时，由代码补 `stop(reason=step_limit)`（§2.3）。

本模块只**声明契约**，不含执行逻辑——执行在 lead_agent 的循环里分派
（观察 → lead_observe；执行 → lead_dispatch；终止 → 循环出口）。
好处：工具表可以独立被审计和测试，不需要把整个循环跑起来。
"""
from __future__ import annotations

# ── 硬边界：写工具清单（与 services/canvas_tools.py 的真实声明逐名对齐）──
WRITE_TOOL_NAMES: frozenset[str] = frozenset({
    "add_chart_block",
    "add_text_block",
    "update_chart_block",
    "remove_block",
    "clear_canvas",
})

OBSERVE_TOOL_NAMES: frozenset[str] = frozenset({
    "get_canvas_layout",
    "list_datasources",
    "list_fields",
    "list_metrics",
    "query_sample",
})
DISPATCH_TOOL_NAMES: frozenset[str] = frozenset({"dispatch_analysis", "repair"})
# P2.5 护栏（§6 不变量 2b）：repair 本质是"补缺口"，需要**独立上限**——
# 否则"补一下 → 仍不达标 → 再补"会把派发额度吃光，且掩盖"方向本身错了"。
REPAIR_TOOL_NAMES: frozenset[str] = frozenset({"repair"})
# repair_kind → 执行内核（§4 P2.5：它**内含路由意图**，不需要新表）
REPAIR_KIND_KERNEL: dict[str, str] = {"patch": "react", "redo": "orchestrator"}
TERMINAL_TOOL_NAMES: frozenset[str] = frozenset({"answer", "ask_user", "stop"})

# 每类工具的次数上限（§2.3 循环闸）。终止动作不占配额——它结束循环。
QUOTA: dict[str, int] = {"observe": 2, "dispatch": 3, "repair": 2}


def _fn(name: str, description: str, properties: dict | None = None,
        required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


OBSERVE_SPECS: list[dict] = [
    _fn("get_canvas_layout", "查看画布现状：已落成哪些块、什么图表、布局如何。不需要参数。"),
    _fn("list_datasources",
        "列出可用数据源（id / 名称 / 类型 / 表级信息）。需要字段时再调 list_fields。"),
    _fn("list_fields", "获取指定数据源的完整字段与类型。",
        {"datasource_id": {"type": "string", "description": "数据源 id（来自 list_datasources）"}},
        ["datasource_id"]),
    _fn("list_metrics", "列出指标语义层的指标口径（名称、公式、单位）。"),
    _fn("query_sample", "取指定数据源的 5 行样本数据，用于确认数据实际形态。",
        {"datasource_id": {"type": "string", "description": "数据源 id"}},
        ["datasource_id"]),
]

DISPATCH_SPECS: list[dict] = [
    _fn("dispatch_analysis",
        "派发一次分析 / 画布子任务（内部完成规划、取数与落块）。"
        "goal 用一句自然语言写清本阶段目标。",
        {"goal": {"type": "string", "description": "本阶段目标"},
         "complexity": {"type": "string", "enum": ["complex", "simple"],
                        "description": "complex=需要多步编排/多图表；simple=单查询或已有上下文可直接答"}},
        ["goal"]),
    _fn("repair",
        "小缺口轻量补发：复用画布已有块，只补缺的部分（不重做全量编排）。",
        {"gap": {"type": "string",
                 "description": "具体缺什么（缺哪个维度 / 哪个数字未证实 / 缺哪种图表）"},
         "goal": {"type": "string",
                  "description": "补发目标，写清只补 X、不要重做 Y"}},
        ["gap", "goal"]),
]

TERMINAL_SPECS: list[dict] = [
    _fn("answer", "直接作答并结束本轮。把要说的内容放进 text。",
        {"text": {"type": "string"}}, ["text"]),
    _fn("ask_user", "反问用户并结束本轮（等用户下一轮回复）。",
        {"question": {"type": "string"},
         "ask_kind": {"type": "string", "enum": ["confirm", "clarify"],
                      "description": "confirm=是/否确认执行某具体操作；clarify=需要用户补充具体信息"}},
        ["question"]),
    _fn("stop", "主目标已完成，结束本轮。", {"reason": {"type": "string"}}, []),
]


def lead_tool_specs() -> list[dict]:
    """注册给 Lead 的完整工具表（顺序：观察 → 执行 → 终止）。"""
    return [*OBSERVE_SPECS, *DISPATCH_SPECS, *TERMINAL_SPECS]


def registered_tool_names() -> set[str]:
    return {s["function"]["name"] for s in lead_tool_specs()}


def tool_class(name: str) -> str | None:
    """工具所属类别：observe / dispatch / terminal；未知返回 None。"""
    if name in OBSERVE_TOOL_NAMES:
        return "observe"
    if name in REPAIR_TOOL_NAMES:
        return "repair"          # 先于 dispatch 判断：repair 有独立配额（§6 2b）
    if name in DISPATCH_TOOL_NAMES:
        return "dispatch"
    if name in TERMINAL_TOOL_NAMES:
        return "terminal"
    return None
