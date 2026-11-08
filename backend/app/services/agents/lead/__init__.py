"""主导 Agent（LeadAgent）包：**单 Agent 循环**是唯一执行路径。

- LeadContext / LeadAgent：唯一对用户负责的主导 Agent（入口在 LeadAgent.stream →
  _stream_unified → lead_agent_unified.stream_unified）
- 工具层：lead_agent_tools（声明） + lead_observe（观察执行） + lead_dispatch（派发）
- 内核选择：lead_worker.ENTRY_WORKERS（入口 × 复杂度 → 执行内核）
- 评审：lead_review / lead_review_stage（HARD 代码闸 + SOFT 一次裁量）
- 感知汇报：perceive_stream（把编排器事件流旁路翻译为 StepProgress）
- run_analysis()：委托确定性编排器执行复杂分析（工具形态）

历史：本包原先还导出 classify_intent / decide_action / decide_action_merged /
MergedOutcome —— 那是 legacy「意图 → 合并决策 → 独立决策 → 回答」四工位的入口，
已随 legacy 一起删除（见 docs/SUPER_OPTIMIZATION_MVP.md）。
"""
from app.services.agents.lead.lead_intent import IntentResult, IntentType
from app.services.agents.lead.lead_decider import ActionType, Decision
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
    "ActionType",
    "Decision",
    "StepProgress",
    "perceive_stream",
    "render_progress_text",
    "RunAnalysisArgs",
    "RunAnalysisResult",
    "run_analysis",
    "LeadAgent",
    "LeadContext",
]
