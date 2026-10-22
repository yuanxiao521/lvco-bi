# -*- coding: utf-8 -*-
"""理解层标签（intent）：请求级一次，属于"用户这一轮想要什么产物"。

## 为什么需要独立于 decision

单 Agent 化把 legacy 的"意图识别 + 决策"合并成了一次调用，IntentResult 整个类型
在 unified 路径里不再构造。但它身上扛着 **5 个用途**，其中 4 个被新机制接管：

| # | legacy 用途 | 单 Agent 接管者 |
|---|---|---|
| 1 | 路由强约束（analysis/canvas_edit 必须执行） | 模型直接选工具（dispatch/repair/answer） |
| 2 | complexity 兜底 | effective_complexity(goal, ..., entry) |
| 3 | LLM 失败时的兜底方向 | 循环闸（无工具调用 → 文本/stop） |
| 4 | needs_plan | 不需要（planner 在 worker 里） |
| 5 | **UI 徽章 / 统计口径** | **无接管者 ← 本模块补的就是这一层** |

## 分层契约（别混）

| 层 | 语义 | 粒度 | 事件 |
|---|---|---|---|
| **理解层（本模块）** | 用户这一轮要什么产物 | 请求级 1 条 | intent |
| **执行层** | 这一步做什么、为什么 | 步级 N 条 | decision |

intent **不参与路由**：路由仍由"模型选工具 + effective_complexity"决定。
它是**标签**（展示 + 监控"理解漂移"），不是第二套决策权——这条边界必须守住，
否则又回到"两套判断互相打架"的老路。

## 两个来源（都必须是真实判断，不回填模板话术）

- model：模型在决策类工具参数里自述（method a，与 reason 同源）；
- derived：模型没给时按 动作 + 入口 + 复杂度 推导，**必须标 source=derived**，
  前端展示"（推断）"——绝不冒充满分事实。
"""
from __future__ import annotations

# 值域沿用 legacy 的 IntentType 五值（前端 INTENT_LABEL 已有映射，无需改词表）
INTENT_VALUES: tuple[str, ...] = ("chat", "data_qa", "canvas_edit", "analysis", "followup")

# 宽松归一：模型偶尔用别名/中文（schema 里是 enum，正常不会出现；这里只做兜底，
# **不做语义猜测**——认不出来就返回 None，交给 derived 兜底，绝不硬凑一个标签）。
_ALIASES: dict[str, str] = {
    "data_query": "data_qa",
    "dataqa": "data_qa",
    "data": "data_qa",
    "question": "data_qa",
    "canvas": "canvas_edit",
    "canvasop": "canvas_edit",
    "edit": "canvas_edit",
    "chart": "canvas_edit",
    "analyze": "analysis",
    "analysis_report": "analysis",
    "follow_up": "followup",
    "followup_question": "followup",
    "chitchat": "chat",
    "chit_chat": "chat",
    "闲聊": "chat",
    "数据问答": "data_qa",
    "画布操作": "canvas_edit",
    "数据分析": "analysis",
    "追问": "followup",
}


def normalize_intent(raw) -> str | None:
    """把模型自述的 intent 归一到五值；认不出返回 None（不猜）。"""
    value = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not value:
        return None
    if value in INTENT_VALUES:
        return value
    return _ALIASES.get(value)


def derive_intent(
    *,
    entry: str,
    action: str,
    complexity: str = "",
    dispatched: bool | None = None,
) -> str:
    """确定性推导（仅在模型没自述时使用）：动作 + 入口 + 复杂度 → 五值之一。

    判据刻意保持"机械"——它只是标签兜底，不是意图识别：
      · 画布入口派发/补发    → canvas_edit（在改画布）
      · 对话入口 complex 派发 → analysis（多步分析）
      · 对话入口 simple 派发  → data_qa（单次数值问答）
      · 只回答/反问/收尾     → chat（没有派发动作，按"对话"记）

    followup 不在此推导：legacy 里它只用于"确认卡片恢复"那条路径（由代码直接构造），
    靠文本特征猜"这是不是追问"必然误判，宁可缺失也不造假。
    """
    if dispatched is None:
        dispatched = action == "call_analysis"
    if dispatched:
        if entry == "canvas":
            return "canvas_edit"
        return "analysis" if complexity == "complex" else "data_qa"
    return "chat"
