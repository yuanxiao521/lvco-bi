"""阶段 6 监控增强验证（Supervisor 监控面）。

背景：原 round 0 的 turn_summaries 只记录"分析完成"一句话（240 字截断），
Lead 无从判断子任务"做了什么、成败如何、多少行"。本阶段：

- `run_analysis` 顺带收集工具调用链（tool_chain：工具名 + 参数摘要 + 行数 + 成败 + 落块数）；
- `LeadAgent._build_execution_summary` 把执行产物组装成结构化摘要回填 turn_summaries，
  Supervisor 下一轮决策据此判断"达没达成"，而不是盲目重复派发。

本文件测试：
1. `_build_execution_summary` 成功路径：概况 / 规划 / 工具链 / 数值证据 / 报告全文。
2. `_build_execution_summary` 失败路径：错误行 + 无工具链段。
3. 真实 `run_analysis` 主循环：tool_call → tool_result 配对收集（ok / rows / args 摘要），
   落块计数、数值去伪、memo 幂等。
4. 端到端 LeadAgent.stream：结构化摘要确实回写 ctx.turn_summaries，供决策器可见。
"""
import json

import pytest

from app.services.agents.lead.lead_agent import LeadAgent, LeadContext
from app.services.agents.lead.lead_tools import (
    RunAnalysisArgs,
    RunAnalysisResult,
    assess_subtask,
    make_idempotency_key,
    run_analysis,
)
from app.services.canvas_tools import (
    CANVAS_TOOL_NAMES,
    GetCanvasLayoutTool,
    render_canvas_layout,
)


# ── 1/2. _build_execution_summary 纯函数（零依赖，确定性）────────────────────

def test_execution_summary_success_with_tool_chain():
    """成功：概况 + 规划 + 工具链（✓/✗/行数）+ 数值证据 + 报告全文。"""
    result = RunAnalysisResult(
        success=True,
        report="报告：总销售额 100 元，环比增长 5%。",
        steps=[{"title": "查询总销售额"}, {"title": "绘制柱状图"}],
        blocks_added=2,
        report_source="orchestrator",
        elapsed_ms=350,
        verified=True,
        tool_chain=[
            {"name": "query_sql", "args": "sql=SELECT sum(amount)…limit=5", "ok": True, "rows": 1},
            {"name": "add_chart_block", "args": "title=销售额柱状图", "ok": True, "rows": None},
            {"name": "query_sql", "args": "sql=SELECT * FROM bad_table", "ok": False, "rows": None},
        ],
    )

    summary = LeadAgent._build_execution_summary(result)

    assert summary.startswith("[子任务执行记录]")
    assert "动作：分析完成，在画布上新增 2 个内容块。" in summary
    assert "规划[2步]" in summary and "查询总销售额" in summary
    # 工具链：名称 + 参数摘要 + 行数 + 成败标记
    assert "工具链：query_sql(" in summary
    assert "✓" in summary and "✗" in summary
    assert ",1行" in summary and ",2行" not in summary  # 无 rows 的不带行数
    # 数值证据
    assert "数值：3 次工具调用，报告数字 已验证。" in summary
    # 报告全文（不再砍到 240 字）
    assert "[子任务产出报告]" in summary
    assert "总销售额 100 元" in summary


def test_execution_summary_failure_case():
    """失败：动作行 + 错误原因；无工具链/报告段。"""
    result = RunAnalysisResult(
        success=False,
        report="",
        error="query_sql: 表不存在",
        report_source="orchestrator",
        elapsed_ms=80,
        verified=True,
        tool_chain=[
            {"name": "list_datasources", "args": "", "ok": True, "rows": 3},
            {"name": "query_sql", "args": "…", "ok": False, "rows": None},
        ],
    )

    summary = LeadAgent._build_execution_summary(result)

    assert "动作：分析未完成。原因：query_sql: 表不存在。" in summary
    assert "工具链：" in summary
    assert "错误：query_sql: 表不存在" in summary
    # 无报告时不应拼 [子任务产出报告] 空段
    assert "[子任务产出报告]" not in summary


# ── 3. 真实 run_analysis 主循环：工具链收集 / 落块 / 数值 / memo ─────────────

class _FakeOrchestrator:
    """替身编排器：按预定事件序列产出，可能抛错后仍补 error 事件。"""

    launches = 0

    def __init__(self, llm, db_session, extra_plannable_tools=None) -> None:  # noqa: ANN001
        self._events = [
            {"type": "plan", "plan": {"steps": [{"title": "查总销售额"}, {"title": "画柱状图"}]}},
            {"type": "tool_call", "name": "query_sql",
             "args": {"sql": "SELECT sum(amount) FROM orders", "limit": 5}},
            {"type": "tool_result", "name": "query_sql",
             "result": json.dumps({"rows": [{"total": 100}], "total": 100})},
            {"type": "text", "content": "报告：总销售额 100 元。", "report_source": "orchestrator"},
            {"type": "tool_call", "name": "add_chart_block",
             "args": {"title": "销售额柱状图", "block_type": "bar"}},
            {"type": "tool_result", "name": "add_chart_block",
             "result": json.dumps({"canvas_action": {"add": True}, "block_id": "b1"})},
            # 失败工具：ok=False，不贡献落块
            {"type": "tool_call", "name": "query_sql", "args": {"sql": "SELECT * FROM bad_table"}},
            {"type": "tool_result", "name": "query_sql",
             "result": json.dumps({"error": "表不存在"})},
        ]

    async def execute_task(self, **kw):  # noqa: ANN001
        _FakeOrchestrator.launches += 1
        for ev in self._events:
            yield ev


async def test_run_analysis_collects_tool_chain_and_memo(monkeypatch):
    """真实 run_analysis：工具链配对（call→result 按名匹配）、ok/rows/args、
    落块计数、数值核对、memo 幂等（二次调用不再启动执行器）。"""
    import app.services.agents as agents_mod

    monkeypatch.setattr(agents_mod, "AgentOrchestrator", _FakeOrchestrator)
    _FakeOrchestrator.launches = 0

    emitted: list[dict] = []

    async def _emit(ev: dict) -> None:
        emitted.append(ev)

    class _NoLLM:
        async def complete(self, *a, **kw):  # noqa: ANN001
            raise AssertionError("不应走到 LLM 复核")

    args = RunAnalysisArgs(goal="测试分析", entry="chat", constraints={"mode": "orchestrator"})
    memo: dict = {}
    ctx = LeadContext(user_id="u1", session_id="s1")

    result = await run_analysis(
        args, db_session=None, emit=_emit, llm=_NoLLM(),
        lead_ctx=ctx, available_datasources=[], memo=memo,
    )

    assert result.success is True
    assert result.blocks_added == 1          # 只有 canvas_action 那条计入
    assert result.report_source == "orchestrator"
    assert "总销售额 100 元" in result.report

    # 工具链：3 条，call→result 按名配对，args 紧凑摘要 + 行数 + 成败
    assert [t["name"] for t in result.tool_chain] == ["query_sql", "add_chart_block", "query_sql"]
    ok0, ok1, ok2 = result.tool_chain
    assert ok0["ok"] is True and ok0["rows"] == 1 and "sum(amount)" in ok0["args"]
    assert ok1["ok"] is True and ok1["rows"] is None and "柱状图" in ok1["args"]
    assert ok2["ok"] is False and ok2["rows"] is None

    # 数值后验：报告含 100，结果集也含 100 → 无未证实数字（llm 从未被调用）
    assert result.verified is True

    # memo 幂等：同 args 二次调用直接复用缓存，执行器不重启
    again = await run_analysis(
        args, db_session=None, emit=_emit, llm=_NoLLM(),
        lead_ctx=ctx, available_datasources=[], memo=memo,
    )
    assert again is result
    assert _FakeOrchestrator.launches == 1
    assert make_idempotency_key(args) in memo


# ── 4. 端到端：结构化摘要回写 turn_summaries，Supervisor 决策可见 ─────────────

class _MergedCallAnalysisThenStopLLM:
    """合并调用返回 call_analysis；第 2 次决策（round 1）返回 stop。"""

    def __init__(self) -> None:
        self.complete_calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.complete_calls += 1
        if self.complete_calls == 1:
            return json.dumps({
                "intent": "analysis", "confidence": 0.95, "needs_plan": True, "slots": {},
                "action": "call_analysis", "tool_name": "run_analysis",
                "tool_args": {"goal": "分析销售额"}, "reason": "多步分析任务",
                "complexity": "complex",
            }, ensure_ascii=False)
        return json.dumps({
            "intent": "analysis", "confidence": 0.9, "needs_plan": False, "slots": {},
            "action": "stop", "reason": "子任务已完成，主目标达成", "complexity": "simple",
        }, ensure_ascii=False)

    async def stream_chat(self, messages, **kw):  # noqa: ANN001
        yield "兜底回答"


async def test_lead_agent_backfills_structured_execution_summary(monkeypatch):
    """LeadAgent.stream 走 call_analysis → round1 stop：turn_summaries 注入
    [子任务执行记录]（含工具链/行数/报告全文），下一次决策输入即可感知效果。"""
    import app.services.agents.lead.lead_agent as mod

    async def _fake_run_analysis(args, *, emit, **kw):  # noqa: ANN001
        await emit({"type": "status", "message": "开始分析"})
        await emit({"type": "tool_call", "name": "query_sql", "args": {"sql": "SELECT 1"}})
        await emit({"type": "tool_result", "name": "query_sql",
                    "result": json.dumps({"rows": [{"v": 100}], "total": 100})})
        await emit({"type": "text", "content": "报告：总销售额 100 元。", "report_source": "orchestrator"})
        return RunAnalysisResult(
            success=True,
            report="报告：总销售额 100 元。",
            steps=[{"title": "查数"}],
            blocks_added=1,
            report_source="orchestrator",
            elapsed_ms=9,
            verified=True,
            tool_chain=[
                {"name": "query_sql", "args": "sql=SELECT 1", "ok": True, "rows": 1},
                {"name": "add_text_block", "args": "", "ok": True, "rows": None},
            ],
        )

    monkeypatch.setattr(mod, "run_analysis", _fake_run_analysis)

    agent = LeadAgent(llm=_MergedCallAnalysisThenStopLLM())
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")

    events = [ev async for ev in agent.stream("帮我分析销售额", ctx=ctx, db_session=None)]

    # 决策循环：round0 call_analysis → round1 stop → break；done 收尾
    decisions = [e for e in events if e["type"] == "decision"]
    assert [d["action"] for d in decisions] == ["call_analysis", "stop"]
    assert events[-1]["type"] == "done"

    # 关键：结构化摘要已回写 turn_summaries，供下一轮决策 prompt 注入
    exec_records = [s for s in ctx.turn_summaries if "[子任务执行记录]" in s]
    assert len(exec_records) == 1
    rec = exec_records[-1]
    assert "动作：分析完成，在画布上新增 1 个内容块。" in rec
    assert "工具链：query_sql(sql=SELECT 1),1行✓" in rec
    assert "✓" in rec and "数值：2 次工具调用，报告数字 已验证。" in rec
    assert "[子任务产出报告]" in rec and "总销售额 100 元" in rec

    # 事件透传不被吞：工具线索/报告仍在流里
    assert any(e["type"] == "tool_call" and e["name"] == "query_sql" for e in events)
    assert any(e["type"] == "report" for e in events)


async def test_turn_summaries_injected_into_next_round_decision(monkeypatch):
    """决策 prompt 确实携带结构化摘要（decide_action round≥1 的 user_content 命中
    【已完成子任务摘要】且包含工具链文本），避免"重派同一任务"。"""
    captured: list[str] = []

    class _CaptureLLM(_MergedCallAnalysisThenStopLLM):
        async def complete(self, messages, **kw) -> str:  # noqa: ANN001
            user_content = next(
                (m["content"] for m in messages if m.get("role") == "user"), ""
            )
            captured.append(str(user_content))
            return await super().complete(messages, **kw)

    import app.services.agents.lead.lead_agent as mod

    async def _fake_run_analysis(args, *, emit, **kw):  # noqa: ANN001
        await emit({"type": "text", "content": "报告：总销售额 100 元。", "report_source": "orchestrator"})
        return RunAnalysisResult(
            success=True,
            report="报告：总销售额 100 元。",
            blocks_added=0,
            report_source="orchestrator",
            tool_chain=[{"name": "query_sql", "args": "SQL", "ok": True, "rows": 1}],
        )

    monkeypatch.setattr(mod, "run_analysis", _fake_run_analysis)

    agent = LeadAgent(llm=_CaptureLLM())
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")
    events = [ev async for ev in agent.stream("帮我分析销售额", ctx=ctx, db_session=None)]

    assert events[-1]["type"] == "done"
    # 第 2 次（round1 决策）输入应带上结构化摘要（工具链证据）
    assert len(captured) >= 2
    round1_prompt = captured[1]
    assert "[子任务执行记录]" in round1_prompt
    assert "query_sql" in round1_prompt


# ── 5. 画布整体布局感知：get_canvas_layout 工具 + Lead 决策/摘要可见 ─────────

def test_canvas_layout_renderer_stats_and_overlap():
    """渲染：块统计 / 图表清单（含坐标）/ 文本数量 / 重叠检测 / 空画布。"""
    blocks = [
        {"id": "b1", "type": "chart", "title": "销售额柱状图", "chartType": "bar",
         "x": 0, "y": 0, "width": 400, "height": 300},
        {"id": "b2", "type": "chart", "title": "订单量趋势", "chartType": "line",
         "x": 50, "y": 50, "width": 400, "height": 300},   # 与 b1 重叠
        {"id": "b3", "type": "text", "blockType": "h1", "content": "销售分析报告"},
    ]
    text = render_canvas_layout(blocks, canvas_id="c1")

    assert "画布 id：c1" in text
    assert "画布当前共有 3 个块：图表 2 个、文本 1 个" in text
    assert "销售额柱状图（bar）" in text and "pos=(0,0)" in text
    assert "销售分析报告" in text
    assert "块重叠" in text and "arrange_layout" in text

    # 块稳定 id 必须出现在快照里：Lead/ReAct 才能据此调 update_chart_block/remove_block
    assert "id=b1" in text
    assert "id=b3" in text

    # 不重叠场景：无重叠提示
    blocks[1]["x"] = 500
    text2 = render_canvas_layout(blocks)
    assert "块重叠" not in text2

    # 空画布
    assert "画布为空" in render_canvas_layout([])
    assert "画布为空" in render_canvas_layout(None)


def test_get_canvas_layout_registered_in_whitelist_and_registry():
    """get_canvas_layout 已进画布工具白名单，且 ToolRegistry 有 schema 供 LLM 感知。"""
    assert "get_canvas_layout" in CANVAS_TOOL_NAMES
    schemas = GetCanvasLayoutTool().schema()
    assert schemas["function"]["name"] == "get_canvas_layout"
    assert "canvas_id" in schemas["function"]["parameters"]["properties"]


def test_execution_summary_includes_canvas_landscape():
    """执行摘要新增『画布现状』段：Lead 一眼看到"画布上落了什么"。"""
    result = RunAnalysisResult(
        success=True,
        report="报告：已生成 2 个图表。",
        blocks_added=2,
        report_source="orchestrator",
        verified=True,
        tool_chain=[{"name": "add_chart_block", "args": "title=销售额柱状图", "ok": True, "rows": None}],
    )
    layout = (
        "画布 id：c1\n画布当前共有 2 个块：图表 2 个、文本 0 个。\n"
        "图表清单：\n- 销售额柱状图（bar）\n- 区域分布饼图（pie）"
    )
    summary = LeadAgent._build_execution_summary(result, canvas_layout=layout)

    assert "画布现状：画布 id：c1；画布当前共有 2 个块：图表 2 个、文本 0 个" in summary
    assert "销售额柱状图" in summary

    # 无画布快照时不产出该段
    assert "画布现状" not in LeadAgent._build_execution_summary(result)


async def test_canvas_entry_injects_snapshot_into_decision_prompt(monkeypatch):
    """画布入口：Lead 决策 prompt 注入【当前画布状态】（DB 落盘快照），
    Supervisor 据此知道"完成了什么、落的什么图表"。"""
    captured: list[str] = []

    class _CaptureLLM(_MergedCallAnalysisThenStopLLM):
        async def complete(self, messages, **kw) -> str:  # noqa: ANN001
            user_content = next(
                (m["content"] for m in messages if m.get("role") == "user"), ""
            )
            captured.append(str(user_content))
            return await super().complete(messages, **kw)

    import app.services.agents.lead.lead_agent as mod

    fake_snapshot = (
        "画布 id：c1\n画布当前共有 3 个块：图表 2 个、文本 1 个。\n"
        "图表清单：\n- 销售额柱状图（bar）\n- 城市分布饼图（pie）"
    )
    async def _fake_snapshot(*a, **kw):  # noqa: ANN002
        return fake_snapshot

    monkeypatch.setattr(mod, "_load_canvas_snapshot", _fake_snapshot)

    async def _fake_run_analysis(args, *, emit, **kw):  # noqa: ANN001
        await emit({"type": "text", "content": "报告：总销售额 100 元。", "report_source": "orchestrator"})
        return RunAnalysisResult(
            success=True,
            report="报告：总销售额 100 元。",
            blocks_added=1,
            report_source="orchestrator",
            tool_chain=[{"name": "add_chart_block", "args": "title=销售额柱状图", "ok": True, "rows": None}],
        )

    monkeypatch.setattr(mod, "run_analysis", _fake_run_analysis)

    agent = LeadAgent(llm=_CaptureLLM())
    ctx = LeadContext(user_id="u1", session_id="s1", entry="canvas", canvas_id="c1")
    events = [ev async for ev in agent.stream("帮我分析销售额", ctx=ctx, db_session=object())]

    assert events[-1]["type"] == "done"
    assert len(captured) >= 2
    # 首轮合并调用 + 第 2 轮决策：都带【当前画布状态】与落盘快照
    for prompt in captured:
        assert "【当前画布状态】" in prompt
        assert fake_snapshot.splitlines()[1] in prompt  # 块统计行


# ── 6. C 方案：规则评审（assess_subtask） + Lead 指导意见（guidance） ────────

def test_assess_subtask_detects_pass_and_failures():
    """确定性评审：达标 / 失败工具 / 数字存疑 / 执行失败 / 画布零落块。"""
    ok = RunAnalysisResult(success=True, verified=True,
                           tool_chain=[{"name": "query_sql", "ok": True}])
    assert "上轮达标" in assess_subtask(ok)

    # 存在失败工具 → 未达标
    bad = RunAnalysisResult(success=True, verified=True,
                            tool_chain=[{"name": "query_sql", "ok": False}])
    txt = assess_subtask(bad)
    assert "未达标" in txt and "失败工具" in txt and "query_sql" in txt

    # 数字存疑 → 未达标
    assert "未达标" in assess_subtask(RunAnalysisResult(success=True, verified=False, tool_chain=[]))

    # 执行失败 → 未达标
    err = assess_subtask(RunAnalysisResult(success=False, error="查询超时", verified=True, tool_chain=[]))
    assert "未达标" in err and "查询超时" in err

    # 画布入口成功但零落块 → 未达标
    zero = RunAnalysisResult(success=True, verified=True, tool_chain=[], blocks_added=0)
    assert "未达标" in assess_subtask(zero, entry="canvas")
    assert "上轮达标" in assess_subtask(zero, entry="chat")  # 对话入口无此约束


def test_merged_decision_parses_guidance():
    """合并决策输出含 guidance → Decision.guidance 被保留。"""
    from app.services.agents.lead.lead_decider import decide_action_merged

    class _LLM:
        async def complete(self, messages, **kw) -> str:
            return json.dumps({
                "intent": "analysis", "confidence": 0.9, "needs_plan": True, "slots": {},
                "action": "call_analysis", "tool_name": "run_analysis",
                "tool_args": {"goal": "重做销售额分析"}, "reason": "上轮工具失败，需要重做",
                "complexity": "complex",
                "guidance": "上轮 query_sql 在 bad_table 上失败；本轮改用 orders 表，先 list_fields 核字段再查。",
            }, ensure_ascii=False)

    outcome, _ = None, None
    import asyncio
    outcome = asyncio.run(decide_action_merged(
        "重做", llm=_LLM(), timeout=5.0, degradation=[],
    ))
    assert outcome.decision.guidance
    assert "bad_table" in outcome.decision.guidance


async def test_worker_guidance_forwarded_to_run_analysis(monkeypatch):
    """Lead 的 guidance 确实随 decision 传到 run_analysis。"""
    import app.services.agents.lead.lead_agent as mod

    captured = {}

    async def _fake_run_analysis(args, *, worker_guidance=None, **kw):  # noqa: ANN001
        captured["guidance"] = worker_guidance or ""
        return RunAnalysisResult(
            success=True, verified=True, report="OK",
            tool_chain=[{"name": "query_sql", "args": "", "ok": True, "rows": 1}],
        )

    monkeypatch.setattr(mod, "run_analysis", _fake_run_analysis)

    class _GuidanceLLM(_MergedCallAnalysisThenStopLLM):
        async def complete(self, messages, **kw) -> str:
            if self.complete_calls == 0:
                return json.dumps({
                    "intent": "analysis", "confidence": 0.9, "needs_plan": True, "slots": {},
                    "action": "call_analysis", "tool_name": "run_analysis",
                    "tool_args": {"goal": "重做分析"}, "reason": "上轮失败需重做",
                    "complexity": "complex", "guidance": "改用 orders 表重查",
                }, ensure_ascii=False)
            return await super().complete(messages, **kw)

    agent = LeadAgent(llm=_GuidanceLLM())
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")
    events = [ev async for ev in agent.stream("分析销售额", ctx=ctx, db_session=None)]
    assert events[-1]["type"] == "done"
    assert captured.get("guidance") == "改用 orders 表重查"


async def test_assessment_injected_into_next_decision_prompt(monkeypatch):
    """[子任务评估] 写入 turn_summaries → 下一轮决策 prompt 可见【上一轮子任务评估】。"""
    captured: list[str] = []

    class _CaptureLLM2(_MergedCallAnalysisThenStopLLM):
        async def complete(self, messages, **kw) -> str:
            user_content = next((m["content"] for m in messages if m.get("role") == "user"), "")
            captured.append(str(user_content))
            return await super().complete(messages, **kw)

    import app.services.agents.lead.lead_agent as mod

    async def _fake_run_analysis(args, *, emit, **kw):  # noqa: ANN001
        await emit({"type": "text", "content": "报告：销售额下降。", "report_source": "orchestrator"})
        return RunAnalysisResult(
            success=True, verified=False,  # 数字存疑 → 规则判定未达标
            report="报告：销售额下降 500%。",
            tool_chain=[{"name": "query_sql", "args": "", "ok": True, "rows": 1}],
        )

    monkeypatch.setattr(mod, "run_analysis", _fake_run_analysis)

    agent = LeadAgent(llm=_CaptureLLM2())
    ctx = LeadContext(user_id="u1", session_id="s1", entry="chat")
    events = [ev async for ev in agent.stream("帮我分析销售额", ctx=ctx, db_session=None)]
    assert events[-1]["type"] == "done"

    # turn_summaries 含规则评估
    assessments = [s for s in ctx.turn_summaries if s.startswith("[子任务评估]")]
    assert assessments and "未达标" in assessments[-1]
    # round1 决策 prompt 注入【上一轮子任务评估】段
    assert len(captured) >= 2
    assert "【上一轮子任务评估】" in captured[1]
    assert "未达标" in captured[1]