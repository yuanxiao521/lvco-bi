"""Agent 层：抽象基类 + 图引擎 + 规划-执行编排器 + 图化 ReAct。

- BaseAgent / AgentResult：Agent 抽象基类与结果类型
- Graph：轻量图编排引擎（LangGraph 模式，零依赖：Node/Edge/条件路由/共享 State/steps_log）
- PlannerAgent：动态任务规划（生成工具调用计划）
- AgentOrchestrator：图化编排器（plan → execute → report / error，复杂任务）
- ReactGraphAgent：图化 ReAct（reason → execute_tools 循环，简单任务/编排降级路径）

执行链路：AgentOrchestrator（复杂任务）与 ReactGraphAgent（简单任务）均基于 Graph 引擎，按复杂度路由。
"""
from app.services.agents.base_agent import BaseAgent, AgentResult
from app.services.agents.graph import Graph
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.agent_orchestrator import AgentOrchestrator
from app.services.agents.react_agent import ReactGraphAgent

__all__ = [
    "BaseAgent",
    "AgentResult",
    "Graph",
    "PlannerAgent",
    "AgentOrchestrator",
    "ReactGraphAgent",
]
