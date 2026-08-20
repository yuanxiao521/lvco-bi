"""Planner 输出 schema 校验（Task 5，P1-7）与动态步骤上限（Task 7，P1-5）测试。

覆盖：
- PlanOutput / PlanStep pydantic 校验：完整 JSON 通过、depends_on 默认值、缺 goal 报 ValidationError
- schema 校验失败 → 带 feedback 重试一次 → 成功返回有效计划（不走 fallback）
- 两次都失败 → 走 fallback 计划
- JSON 解析失败 → 不触发 schema 重试，直接 fallback（原有行为保持不变）
- _estimate_max_steps 动态步骤上限估算（复杂报告 ≥9 / 简单任务 5 / clamp 到 12）
- 动态上限在 PlannerAgent.execute 中对步骤截断的端到端生效

Mock 方式参考 tests/test_agent_flows.py：Fake LLM（async complete），无需数据库与真实 LLM。
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.services.agents.planner_agent import (
    PlanOutput,
    PlanStep,
    PlannerAgent,
    _estimate_max_steps,
)


# ======================================================================
# Mock 基础设施
# ======================================================================


class ScriptedLLM:
    """按脚本依次返回响应的 Mock LLM，记录每次收到的 messages。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("LLM 调用次数超出脚本预期")
        return self.responses.pop(0)


VALID_PLAN = {
    "task_summary": "分析各区域销售趋势",
    "steps": [
        {"step_id": 1, "goal": "查询各区域销售数据", "tool": "query_datasource",
         "depends_on": [], "purpose": "获取数据"},
        {"step_id": 2, "goal": "生成趋势折线图", "tool": "render_chart",
         "depends_on": [1], "purpose": "可视化"},
    ],
    "expected_output": "report",
}

# 缺必填字段 goal → ValidationError
INVALID_PLAN = {
    "task_summary": "缺字段计划",
    "steps": [{"step_id": 1, "tool": "query_datasource", "depends_on": []}],
}


def _make_plan(n_steps: int) -> dict:
    """构造 n_steps 步的完整合法计划。"""
    return {
        "task_summary": f"多步任务（{n_steps} 步）",
        "steps": [
            {"step_id": i, "goal": f"步骤 {i}", "tool": "query_datasource", "depends_on": []}
            for i in range(1, n_steps + 1)
        ],
        "expected_output": "report",
    }


# ======================================================================
# Task 5：PlanOutput schema 校验
# ======================================================================


def test_plan_output_accepts_full_json():
    plan = PlanOutput.model_validate(VALID_PLAN)
    assert plan.task_summary == "分析各区域销售趋势"
    assert len(plan.steps) == 2
    assert plan.steps[0].goal == "查询各区域销售数据"
    assert plan.steps[1].depends_on == [1]
    assert plan.expected_output == "report"


def test_plan_step_depends_on_has_default():
    """缺 depends_on（及 purpose/tool）时使用默认值，应通过校验。"""
    step = PlanStep.model_validate({"step_id": 1, "goal": "查询数据", "tool": "query_datasource"})
    assert step.depends_on == []
    assert step.purpose == ""


def test_plan_output_missing_goal_raises_validation_error():
    """step 缺必填字段 goal → ValidationError。"""
    with pytest.raises(ValidationError):
        PlanOutput.model_validate(INVALID_PLAN)


def test_plan_output_missing_steps_raises_validation_error():
    """缺 steps → ValidationError。"""
    with pytest.raises(ValidationError):
        PlanOutput.model_validate({"task_summary": "只有摘要", "expected_output": "report"})


def test_plan_output_allows_extra_fields():
    """额外字段（extra=allow）不报错，保持对 LLM 输出的兼容。"""
    plan = PlanOutput.model_validate({
        **VALID_PLAN,
        "extra_top": 1,
        "steps": [{**VALID_PLAN["steps"][0], "args": {"sql": "SELECT 1"}}],
    })
    assert plan.steps[0].step_id == 1


# ======================================================================
# Task 5：retry with feedback
# ======================================================================


@pytest.mark.asyncio
async def test_execute_retries_with_feedback_then_succeeds():
    """第一次返回缺字段 JSON、第二次返回完整 JSON → 返回有效计划（不走 fallback）。"""
    llm = ScriptedLLM([
        json.dumps(INVALID_PLAN, ensure_ascii=False),
        json.dumps(VALID_PLAN, ensure_ascii=False),
    ])
    planner = PlannerAgent(llm)

    result = await planner.execute(user_msg="分析各区域销售趋势", history=[], available_datasources=[])

    assert result.success is True
    assert len(llm.calls) == 2  # 首次调用 + 带 feedback 的重试各一次

    # 重试消息在原 messages 基础上追加了 feedback（含具体校验错误）
    assert len(llm.calls[1]) == len(llm.calls[0]) + 1
    feedback_msg = llm.calls[1][-1]
    assert feedback_msg["role"] == "user"
    assert "校验" in feedback_msg["content"]
    assert "goal" in feedback_msg["content"]

    # 返回的是重试后的有效计划，而非 fallback
    assert result.data["task_summary"] == "分析各区域销售趋势"
    assert [s["step_id"] for s in result.data["steps"]] == [1, 2]
    assert result.data["steps"][1]["depends_on"] == [1]


@pytest.mark.asyncio
async def test_execute_valid_plan_no_retry():
    """首次响应即通过 schema 校验 → 不触发重试。"""
    llm = ScriptedLLM([json.dumps(VALID_PLAN, ensure_ascii=False)])
    planner = PlannerAgent(llm)

    result = await planner.execute(user_msg="分析各区域销售趋势", history=[], available_datasources=[])

    assert result.success is True
    assert len(llm.calls) == 1
    assert result.data["task_summary"] == "分析各区域销售趋势"


@pytest.mark.asyncio
async def test_execute_falls_back_after_both_attempts_fail():
    """两次都返回缺字段 JSON → 走 fallback 计划。"""
    bad = json.dumps(INVALID_PLAN, ensure_ascii=False)
    llm = ScriptedLLM([bad, bad])
    planner = PlannerAgent(llm)

    result = await planner.execute(user_msg="分析销售", history=[], available_datasources=[])

    assert result.success is True
    assert len(llm.calls) == 2  # 只重试一次，不再第三次调用
    # fallback 计划特征：task_summary=fallback，单步 list_datasources
    assert result.data["task_summary"] == "fallback"
    assert result.data["steps"][0]["tool"] == "list_datasources"


@pytest.mark.asyncio
async def test_execute_invalid_json_goes_fallback_without_retry():
    """JSON 解析失败（非 JSON 文本）→ 不触发 schema 重试，直接 fallback（原有行为不变）。"""
    llm = ScriptedLLM(["抱歉，我无法生成计划。"])
    planner = PlannerAgent(llm)

    result = await planner.execute(user_msg="分析销售", history=[], available_datasources=[])

    assert result.success is True
    assert len(llm.calls) == 1  # 无 schema 重试
    assert result.data["task_summary"] == "fallback"
    assert result.data["steps"][0]["tool"] == "list_datasources"


# ======================================================================
# Task 7：动态步骤上限
# ======================================================================


def test_estimate_max_steps_complex_report():
    """复杂报告任务：命中 趋势/占比/TOP/归因/报告 等 → ≥9。"""
    msg = "生成含趋势、占比、TOP10、异常归因的完整销售报告"
    assert _estimate_max_steps(msg) >= 9


def test_estimate_max_steps_simple_task():
    """简单展示任务：无意图关键词 → 基础 5。"""
    assert _estimate_max_steps("展示销售数据") == 5


def test_estimate_max_steps_upper_bound():
    """超多关键词（+多数据源）→ clamp 到 ≤12。"""
    msg = "趋势 占比 分布 对比 TOP 排名 归因 分析 报告 报表 多个 综合"
    assert _estimate_max_steps(msg) <= 12
    assert _estimate_max_steps(msg, datasource_count=10) == 12


def test_estimate_max_steps_datasource_bonus():
    """多数据源加成：+min(datasource_count, 3)；单数据源无加成。"""
    assert _estimate_max_steps("展示销售数据", datasource_count=2) == 7
    assert _estimate_max_steps("展示销售数据", datasource_count=9) == 8
    assert _estimate_max_steps("展示销售数据", datasource_count=1) == 5


# ======================================================================
# Task 7：动态上限在 execute 中生效
# ======================================================================


@pytest.mark.asyncio
async def test_execute_dynamic_limit_truncates_complex_task():
    """复杂任务（命中 ≥4 个意图词）→ 上限 9，15 步截断到 9 步。"""
    llm = ScriptedLLM([json.dumps(_make_plan(15), ensure_ascii=False)])
    planner = PlannerAgent(llm)

    result = await planner.execute(
        user_msg="趋势 占比 对比 TOP 综合分析", history=[], available_datasources=[])

    assert result.success is True
    assert len(result.data["steps"]) == 9


@pytest.mark.asyncio
async def test_execute_dynamic_limit_truncates_simple_task():
    """简单任务 → 上限 5，10 步截断到 5 步。"""
    llm = ScriptedLLM([json.dumps(_make_plan(10), ensure_ascii=False)])
    planner = PlannerAgent(llm)

    result = await planner.execute(user_msg="展示销售数据", history=[], available_datasources=[])

    assert result.success is True
    assert len(result.data["steps"]) == 5
