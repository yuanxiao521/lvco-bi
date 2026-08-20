"""编排器 Report 模板化 fallback（Task 1）与超时控制（Task 6）回归测试。

覆盖：
- _generate_template_report：纯模板报告（rows 前 5 行摘要 / chart 说明 / 超时状态 / 默认摘要）
- _generate_report：LLM 异常（RateLimit 风格）或空响应时降级 template，成功时 report_source=llm
- execute_task 全流程：报告降级 report_source=template，无 error 事件
- 单步骤超时：该步骤标记 skipped/timeout，后续步骤仍执行，整体正常 done
- 整体超时：取消未完成步骤，done(timeout=True) + 模板报告兜底

全部使用 Mock LLM，无需数据库与真实 LLM。
"""
from __future__ import annotations

import asyncio
import json

from app.config import settings
from app.services.agents.agent_orchestrator import AgentOrchestrator

PLAN = {
    "task_summary": "测试任务：查询并可视化销售额",
    "steps": [
        {"step_id": 1, "goal": "查询各区域销售额", "tool": "query_datasource",
         "depends_on": [], "purpose": "获取数据"},
        {"step_id": 2, "goal": "生成柱状图", "tool": "render_chart",
         "depends_on": [1], "purpose": "可视化"},
    ],
    "expected_output": "report",
}

DATASOURCE = [{
    "id": "ds1", "name": "测试源", "type": "csv",
    "fields": [{"name": "region"}, {"name": "amount"}],
}]


class RateLimitError(Exception):
    """模拟 LLM 限流异常（openai RateLimitError 风格）。"""


class ReportFailLLM:
    """Planner 正常返回计划；执行步骤直接文本输出；报告 complete 失败（限流/空响应）。"""

    def __init__(self, empty_response: bool = False) -> None:
        self.empty_response = empty_response
        self.report_calls = 0

    async def complete(self, messages, **kwargs):
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "ReportAgent" in text:  # 报告请求
            self.report_calls += 1
            if self.empty_response:
                return ""
            raise RateLimitError("429 rate limit exceeded")
        return json.dumps(PLAN, ensure_ascii=False)  # Planner 请求

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        yield {"type": "text", "content": "步骤完成"}


class ReportOkLLM:
    """Planner / 报告均正常。"""

    async def complete(self, messages, **kwargs):
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "ReportAgent" in text:
            return "## LLM 报告\n\n分析完成。"
        return json.dumps(PLAN, ensure_ascii=False)

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        yield {"type": "text", "content": "步骤完成"}


class StepTimeoutLLM:
    """步骤 1 执行 sleep 超过 AGENT_STEP_TIMEOUT；步骤 2 正常；报告正常。"""

    def __init__(self) -> None:
        self.step2_done = False

    async def complete(self, messages, **kwargs):
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "ReportAgent" in text:
            return "## LLM 报告\n\n分析完成。"
        return json.dumps(PLAN, ensure_ascii=False)

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        text = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
        if "step_id=1" in text:
            await asyncio.sleep(1.0)  # 远超测试中调低的步骤超时（0.1s）
        else:
            self.step2_done = True
        yield {"type": "text", "content": "步骤2完成"}


class SlowStepLLM:
    """Planner 正常；每个执行步骤 sleep 很久，触发整体超时。"""

    async def complete(self, messages, **kwargs):
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "ReportAgent" in text:
            return "## LLM 报告"
        return json.dumps(PLAN, ensure_ascii=False)

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        await asyncio.sleep(5.0)
        yield {"type": "text", "content": "不应到达"}


async def _collect_events(orch: AgentOrchestrator) -> list[dict]:
    events: list[dict] = []
    async for ev in orch.execute_task(
        user_msg="查询各区域销售额并生成柱状图",
        history=[],
        user_id="u1",
        available_datasources=DATASOURCE,
    ):
        events.append(ev)
    return events


def _safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return {}


# ======================================================================
# 测试 A：_generate_template_report 直接单测
# ======================================================================


async def test_generate_template_report_direct():
    orch = AgentOrchestrator(ReportOkLLM(), None)
    rows_result = json.dumps({
        "columns": ["region", "amount"],
        "rows": [[f"区域{i}", i * 10] for i in range(6)],
    }, ensure_ascii=False)
    chart_result = json.dumps({
        "chart_type": "bar",
        "option": {"title": {"text": "销售额"}},
    }, ensure_ascii=False)
    timeout_result = json.dumps({"skipped": True, "timeout": True, "goal": "慢步骤"},
                                ensure_ascii=False)

    report = orch._generate_template_report("销售分析", [
        {"step_id": 1, "goal": "查询各区域销售额", "result": rows_result},
        {"step_id": 2, "goal": "生成柱状图", "result": chart_result},
        {"step_id": 3, "goal": "慢步骤", "result": timeout_result},
        {"step_id": 4, "goal": "未执行到的步骤", "result": ""},
    ])

    # 第一行为任务摘要，结尾为综合说明
    assert report.splitlines()[0] == "# 销售分析"
    assert "以上为系统自动生成的结构化报告" in report
    # 步骤编号 + goal + 状态
    assert "步骤 1：查询各区域销售额" in report
    assert "状态：成功" in report and "状态：超时" in report and "状态：未执行" in report
    # rows/columns：前 5 行关键字段摘要
    assert "region=区域0" in report and "amount=0" in report
    assert "区域5" not in report  # 只展示前 5 行
    assert "共 6 行" in report
    # chart 信息：说明生成了图表
    assert "已生成图表" in report and "bar" in report


async def test_generate_template_report_default_summary():
    orch = AgentOrchestrator(ReportOkLLM(), None)
    report = orch._generate_template_report("", [])
    assert report.splitlines()[0] == "# 任务执行报告"
    assert "以上为系统自动生成的结构化报告" in report


# ======================================================================
# 测试 A：_generate_report 降级路径（mock LLM 失败）
# ======================================================================


async def test_generate_report_fallback_on_llm_exception():
    orch = AgentOrchestrator(ReportFailLLM(), None)
    results = {1: json.dumps({"columns": ["region"], "rows": [["A"]]}, ensure_ascii=False)}

    report, source = await orch._generate_report("查询销售额", PLAN, results)

    assert source == "template"
    assert report.strip()
    assert "查询各区域销售额" in report  # 含步骤摘要
    assert "以上为系统自动生成的结构化报告" in report


async def test_generate_report_fallback_on_empty_response():
    orch = AgentOrchestrator(ReportFailLLM(empty_response=True), None)

    report, source = await orch._generate_report("查询销售额", PLAN, {})

    assert source == "template"
    assert "以上为系统自动生成的结构化报告" in report


async def test_generate_report_llm_success():
    orch = AgentOrchestrator(ReportOkLLM(), None)

    report, source = await orch._generate_report("查询销售额", PLAN, {})

    assert source == "llm"
    assert report == "## LLM 报告\n\n分析完成。"


async def test_execute_task_report_fallback_stream():
    """全流程：Planner/执行正常，报告 LLM 限流异常 → 降级模板报告，事件流带 report_source。"""
    ml = ReportFailLLM()
    orch = AgentOrchestrator(ml, None)
    events = await _collect_events(orch)

    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-1] == "done"
    assert ml.report_calls == 1

    text_events = [e for e in events if e["type"] == "text"]
    assert len(text_events) == 1
    assert text_events[0]["report_source"] == "template"
    content = text_events[0]["content"]
    assert "以上为系统自动生成的结构化报告" in content
    assert "查询各区域销售额" in content  # 含步骤摘要


# ======================================================================
# 测试 B：单步骤超时 → skipped/timeout，后续步骤仍执行，整体正常结束
# ======================================================================


async def test_step_timeout_marks_skipped_and_continues(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_STEP_TIMEOUT", 0.1)  # 加速：步骤超时降到 0.1s
    ml = StepTimeoutLLM()
    orch = AgentOrchestrator(ml, None)
    events = await _collect_events(orch)

    types = [e["type"] for e in events]
    assert "error" not in types
    assert types[-1] == "done"  # 整体正常结束

    # 步骤 1 被标记 skipped/timeout
    tool_results = [e for e in events if e["type"] == "tool_result"]
    skipped = [e for e in tool_results if _safe_json_loads(e.get("result", "")).get("skipped")]
    assert len(skipped) == 1
    parsed = _safe_json_loads(skipped[0]["result"])
    assert parsed.get("skipped") is True
    assert parsed.get("timeout") is True

    # 后续步骤仍执行
    assert ml.step2_done is True
    step2_events = [e for e in tool_results if "步骤2完成" in str(e.get("result", ""))]
    assert step2_events

    # 报告正常走 LLM（未降级）
    text_events = [e for e in events if e["type"] == "text"]
    assert text_events[-1]["report_source"] == "llm"


# ======================================================================
# 整体超时：取消未完成步骤，已完成步骤正常输出，模板报告兜底 + done(timeout)
# ======================================================================


async def test_orchestrator_timeout_emits_template_report_and_done(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_ORCHESTRATOR_TIMEOUT", 0.3)
    monkeypatch.setattr(settings, "AGENT_STEP_TIMEOUT", 30)  # 单步不超时，确保触发整体超时
    ml = SlowStepLLM()
    orch = AgentOrchestrator(ml, None)
    events = await _collect_events(orch)

    types = [e["type"] for e in events]
    assert "error" not in types  # 超时输出 done 而非 error
    assert events[-1] == {"type": "done", "timeout": True}

    # 模板报告兜底
    text_events = [e for e in events if e["type"] == "text"]
    assert len(text_events) == 1
    assert text_events[0]["report_source"] == "template"
    content = text_events[0]["content"]
    assert "以上为系统自动生成的结构化报告" in content
    assert "查询各区域销售额" in content  # 计划中的步骤出现在兜底报告
    assert "状态：未执行" in content  # 被取消的步骤标记未执行
