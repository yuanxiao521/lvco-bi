"""主导 Agent（LeadAgent）：意图识别 / 决策 / 感知汇报 / run_analysis 工具 / 主类。

- LeadContext / LeadAgent：唯一对用户负责的主导 Agent
- classify_intent()：五类意图识别（chat / data_qa / canvas_edit / analysis / followup）
- decide_action()：Supervisor 动作决策（answer / call_analysis / ask_user / stop）
- perceive_stream()：把编排器事件流旁路翻译为 StepProgress
- run_analysis()：委托确定性编排器执行复杂分析（工具形态）

设计文档：`.trae/documents/lead-agent-upgrade-design.md`
学习手册：`.trae/documents/lead-agent-learning-guide.md`
"""
from app.services.agents.lead.lead_intent import IntentType, IntentResult, classify_intent
from app.services.agents.lead.lead_decider import ActionType, Decision, decide_action
from app.services.agents.lead.lead_perception import (
    StepProgress,
    perceive_stream,
    render_progress_text,
)
from app.services.agents.lead.lead_tools import (
    RunAnalysisArgs,
    RunAnalysisResult,
    run_analysis,
)
from app.services.agents.lead.lead_agent import LeadAgent, LeadContext

__all__ = [
    "IntentType",
    "IntentResult",
    "classify_intent",
    "ActionType",
    "Decision",
    "decide_action",
    "StepProgress",
    "perceive_stream",
    "render_progress_text",
    "RunAnalysisArgs",
    "RunAnalysisResult",
    "run_analysis",
    "LeadAgent",
    "LeadContext",
]
