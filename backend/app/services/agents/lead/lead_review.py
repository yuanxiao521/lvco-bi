# -*- coding: utf-8 -*-
"""子任务验收：评审口径、执行摘要、结果话术（纯函数）。

从 `LeadAgent` 抽出的**无实例状态**函数——验收逻辑不该和主循环的控制流缠在一起：

- `review_subtask`：确定性评估（四维判定，实现见 `lead_tools.assess_subtask`）；
- `build_execution_summary`：把执行产物组装成 Supervisor 可读的结构化摘要；
- `compose_result_text`：给用户看的一句"人话"总结。

三者都是纯函数（输入 → 输出，不碰会话状态）：既便于单测，
也便于按场景（画布 / 对话）分别调整验收口径。
"""
from __future__ import annotations

from app.services.agents.lead.lead_tools import (
    RunAnalysisResult,
    SubtaskAssessment,
    assess_subtask,
    assess_subtask_detail,
)

# 规划步骤的展示上限：与 planner 的动态上限（_MAX_PLAN_STEPS=12）对齐——
# 工具链都展示 12 条了，规划没理由只给 6 步；满配计划必须能完整展示。
_MAX_PLAN_SHOWN = 12
# 单步标题截断长度。planner 的 goal ≤200 字、purpose ≤120 字，只取前 40 字做索引。
_STEP_CHARS = 40


def _step_label(step: dict) -> str:
    """取步骤标题。

    **字段名以 planner 的真实产出为准**：规范化后产出的步骤字段是
    step_id / goal / tool / depends_on / purpose / granularity。
    历史上这里读的是 title / content / objective——生产从不产出这三个字段，
    导致摘要里的「规划」一段全部 fallback 成占位符「步骤」（静默失去信息量）。
    保留旧字段名只为向后兼容外部构造的 steps。
    """
    text = (
        step.get("goal")
        or step.get("purpose")
        or step.get("title")
        or step.get("content")
        or step.get("objective")
        or "步骤"
    )
    return str(text)[:_STEP_CHARS]


def review_subtask(result: RunAnalysisResult, *, entry: str, user_goal: str) -> str:
    """对一次子任务做确定性评估（成功/失败都评），产出可注入决策器的评估文本。"""
    return assess_subtask(result, entry=entry, user_goal=user_goal)


def review_subtask_detail(
    result: RunAnalysisResult, *, entry: str, user_goal: str
) -> SubtaskAssessment:
    """评审的**结构化**出口（HARD/SOFT 分级）——P2 评审阶段使用。

    review_subtask 保持原样（返回文本、写入 turn_summaries 的通道不动）。
    """
    return assess_subtask_detail(result, entry=entry, user_goal=user_goal)


def compose_result_text(*, blocks: int, canvas_highlight: str) -> str:
    """给用户看的一句总结（不是工具记录，是"人话"）。"""
    if blocks > 0:
        if canvas_highlight:
            return f"分析完成，已生成 {blocks} 个内容块。要点：{canvas_highlight}（详见画布）"
        return f"分析完成，已在画布上添加了 {blocks} 个内容块，你可以直接查看和调整。"
    return "分析完成，结果已生成，请查看上方内容。"


def build_execution_summary(
    result: RunAnalysisResult,
    canvas_layout: str = "",
) -> str:
    """把一次 run_analysis 的执行产物组装成结构化摘要，供 Supervisor 决策参考。

    三块信息（业界 subagents 模式的 worker 回传口径，但不带完整 SQL/全量结果）：
      1. 子任务完成了什么（成败 + 产物数 + 规划）
      2. 执行链路（tool_chain：工具名 + 参数摘要 + 行数 + 成败）
      3. 证据与报告（数值校验结果 + 报告全文，避免 240 字截断导致 Lead 无法判断"达没达成"）
    canvas_layout：子任务落块后数据库里已落盘的画布布局快照（画布入口传入），
    让 Supervisor 连"画布上现在到底有什么、是什么图表"都一目了然。
    """
    lines: list[str] = ["[子任务执行记录]"]
    # 1. 概况
    if result.success:
        if result.blocks_added > 0:
            lines.append(f"  动作：分析完成，在画布上新增 {result.blocks_added} 个内容块。")
        else:
            lines.append("  动作：分析完成，结果已生成。")
    else:
        lines.append(f"  动作：分析未完成。原因：{result.error or '未知'}。")
    # 2. 规划步骤（若有）
    if result.steps:
        plan_text = " → ".join(_step_label(s) for s in result.steps[:_MAX_PLAN_SHOWN])
        lines.append(f"  规划[{len(result.steps)}步]：{plan_text}")
    # 3. 执行链路（工具 + 参数摘要 + 行数 + 成败）
    if result.tool_chain:
        chain_parts: list[str] = []
        for tc in result.tool_chain[:12]:
            ok_mark = "✓" if tc.get("ok") else "✗"
            rows = f",{tc['rows']}行" if tc.get("rows") is not None else ""
            args = f"({tc.get('args', '')})" if tc.get("args") else ""
            chain_parts.append(f"{tc.get('name','?')}{args}{rows}{ok_mark}")
        lines.append(f"  工具链：{' → '.join(chain_parts)}")
    # 4. 数值证据 + 校验结果
    if result.tool_chain:
        verified_mark = "已验证" if result.verified else "⚠️部分数字未经查询结果证实"
        lines.append(f"  数值：{len(result.tool_chain)} 次工具调用，报告数字 {verified_mark}。")
    # 5. 画布现状（已落盘的真实效果：有多少块、是什么图表/文本、布局）
    if canvas_layout:
        one_line = canvas_layout.replace("\n", "；")
        lines.append(f"  画布现状：{one_line[:400]}")
    if result.error:
        lines.append(f"  错误：{result.error[:200]}")
    summary = "\n".join(lines)
    # 6. 报告全文（truncate 1500，不再砍到 240）
    if result.report:
        report_excerpt = result.report[:1500]
        summary += f"\n[子任务产出报告]\n{report_excerpt}"
    return summary
