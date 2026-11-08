# -*- coding: utf-8 -*-
"""Lead 的动作枚举与决策载荷（**只保留共享类型**）。

历史：本模块原先承载 legacy 的四工位决策（`decide_action` / `decide_action_merged`）：
一次合并调用给意图+决策、失败再走一次独立决策、最后按意图确定性兜底。那条路径已随
legacy 一起删除（见 docs/SUPER_OPTIMIZATION_MVP.md：single_agent 是唯一执行路径）。

现在仍在用的只有两样东西，它们被 **统一循环**（lead_agent_unified）、**派发**
（lead_dispatch）、**确认守卫**（LeadAgent._confirm_guard）共享：

- `ActionType`：动作枚举（answer / call_analysis / ask_user / stop），也是 SSE `decision`
  事件的 action 取值——前端 agentMeta 直接消费，不能改词；
- `Decision`：派发时传给执行层的决策载荷（goal / complexity / guidance / acceptance…）。

为什么还留在这里：它是"决策词汇表"的唯一定义处。工具层（lead_agent_tools.decision_action）
与派发表（lead_worker）都按同一套词翻译，拆散会重新出现"两套命名互译"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActionType(str, Enum):
    """主导 Agent（Supervisor）可执行的动作枚举。

    注意：Supervisor 只决策"做什么任务"，不决策"用哪个执行器"——
    执行器由 entry（入口）确定性决定（见 lead_worker.ENTRY_WORKERS）。
    """

    ANSWER = "answer"                # 直接文本回答
    CALL_ANALYSIS = "call_analysis"  # 派发分析/画布子任务（内核由 entry+复杂度决定）
    ASK_USER = "ask_user"            # 信息不足，反问
    STOP = "stop"                    # 主管收尾：主目标已完成，本轮对话结束


@dataclass
class Decision:
    """决策结果（派发载荷）。"""

    action: ActionType
    tool_name: str | None = None
    tool_args: dict = field(default_factory=dict)
    direct_text: str | None = None
    reason: str = ""
    degraded: bool = False
    # 任务复杂度（供执行器选择内核，避免额外一次分类调用）：
    # "complex" → 对话页走 AgentOrchestrator；"simple" → 对话页走轻量 ReAct。
    # 画布页忽略此字段（入口决定内核）。
    complexity: str = "complex"
    # Lead 指导意见（仅 call_analysis 有意义）：对上一轮子任务的评估 + 本轮要改进/补充什么。
    guidance: str = ""
    # ask_user 的反问类型：confirm = 封闭确认（前端渲染确认卡片）；
    # clarify = 开放澄清。缺省/非法值一律 clarify（保守）。
    ask_kind: str = "clarify"
    # P3：Lead **自己声明**的验收标准（结构化、可核验）。缺失 = 不声明。
    # 注意：它**不进幂等键**——改声明不该击穿 memo。
    acceptance: dict | None = None
