"""Agent 链路回归测试（图引擎 / 编排器 / ReAct / 工具 / 上下文压缩）。

覆盖 Phase 1.6-1.9 + Phase 2 的关键行为：
- 编排器：Planner 骨架 → Executor agentic 执行 → 失败重试（≤3）→ 图表 → 报告；trace 观测统计
- ReAct：reason → execute_tools 循环；trace 汇总元数据
- stats_analyzer：数值/类别统计 + 错误路径
- context_utils：工具结果摘要化（error 不压缩）+ 历史压缩

全部使用 Mock LLM 与 Fake 工具，无需数据库与真实 LLM。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services.observability import TraceRecord


# ======================================================================
# Mock 基础设施
# ======================================================================


class MockOrchestratorLLM:
    """编排器 Mock：Planner 返回骨架计划；Executor 按 step_id 决策；Report 输出文本。

    单步为 mini ReAct 循环（优化后编排器按 LLM 纯文本决策终止步骤，见
    test_orchestrator_advanced 中各 push_tool_call + push_text 的契约）：
      查询步骤：失败 1 次（broken）→ 重试成功（ok）→ 纯文本结束步骤；
      图表步骤：调用 render_chart（携带数据）→ 纯文本结束步骤。
    """

    def __init__(self) -> None:
        self.complete_calls = 0
        self.tool_llm_calls = 0
        self._q_calls = 0  # 查询步骤 LLM 决策次数
        self._c_calls = 0  # 图表步骤 LLM 决策次数

    async def complete(self, messages, **kwargs):
        self.complete_calls += 1
        text = " ".join(str(m.get("content") or "") for m in messages)
        if "step_id" in text:
            return json.dumps({
                "task_summary": "测试任务",
                "steps": [
                    {"step_id": 1, "goal": "查询各区域销售额", "tool": "query_sql", "depends_on": [], "purpose": "获取数据"},
                    {"step_id": 2, "goal": "生成柱状图", "tool": "render_chart", "depends_on": [1], "purpose": "可视化"},
                ],
                "expected_output": "report",
            }, ensure_ascii=False)
        return "## 测试报告\n\n查询完成，共 1 行数据。**销售额 100**"

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        self.tool_llm_calls += 1
        text = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "user")
        if "step_id=2" in text:
            # 图表步骤：先 render_chart，再用纯文本结束步骤
            self._c_calls += 1
            if self._c_calls == 1:
                yield {"type": "tool_call", "name": "render_chart", "id": "call_c1",
                       "arguments": json.dumps({"chart_type": "bar", "title": "销售额",
                                                "columns": ["region", "amount"],
                                                "rows": [["A", 100]]})}
            else:
                yield {"type": "text", "content": ""}  # 纯文本（空）结束图表步骤
        else:
            # 查询步骤：失败 1 次（broken）→ 重试成功（ok）→ 纯文本结束步骤
            self._q_calls += 1
            if self._q_calls == 1:
                yield {"type": "tool_call", "name": "query_sql", "id": "call_q1",
                       "arguments": json.dumps({"datasource_id": "ds1", "sql": "SELECT broken"})}
            elif self._q_calls == 2:
                yield {"type": "tool_call", "name": "query_sql", "id": "call_q2",
                       "arguments": json.dumps({"datasource_id": "ds1", "sql": "SELECT ok"})}
            else:
                yield {"type": "text", "content": ""}  # 纯文本（空）结束查询步骤


class MockReactLLM:
    """ReAct Mock：第一轮工具调用，第二轮最终文本。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "tool_call", "name": "query_sql", "id": "c1",
                   "arguments": json.dumps({"datasource_id": "ds1", "sql": "SELECT region, amount FROM data"})}
        else:
            yield {"type": "text", "content": "## 分析结果\n\n**金额 100**"}


class FakeQueryTool:
    """query_sql Fake：broken SQL 返回错误（含 hint），否则返回 50 行结果。"""

    name = "query_sql"

    def __init__(self) -> None:
        self.calls = 0

    def schema(self):
        return {"type": "function", "function": {"name": "query_sql", "description": "q",
                "parameters": {"type": "object", "properties": {"datasource_id": {"type": "string"},
                               "sql": {"type": "string"}}, "required": ["datasource_id"]}}}

    async def execute(self, datasource_id=None, sql=None, **kwargs):
        self.calls += 1
        if "broken" in str(sql or ""):
            return json.dumps({"error": "Binder Error: column x not found",
                               "hint": "table_ref ds1 columns region amount"}, ensure_ascii=False)
        return json.dumps({"columns": ["region", "amount"], "rows": [[i, i * 10] for i in range(50)],
                           "summary": {"row_count": 50}}, ensure_ascii=False)


class FakeRegistry:
    """可注入的工具注册表替身。"""

    _tools: dict = {}

    @classmethod
    def register(cls, tool) -> None:
        cls._tools[tool.name] = tool

    @classmethod
    def get(cls, name):
        return cls._tools.get(name)

    @classmethod
    def schemas(cls):
        return [t.schema() for t in cls._tools.values()]

    @classmethod
    def reset(cls) -> None:
        cls._tools = {}


class TraceCapture:
    """捕获内部 trace 记录的替身 observer。"""

    def __init__(self) -> None:
        self.trace_record: TraceRecord | None = None

    def trace(self, name, user_id=None, session_id=None, metadata=None):
        from contextlib import contextmanager

        rec = TraceRecord(name=name, user_id=user_id, session_id=session_id,
                          metadata=dict(metadata or {}))
        self.trace_record = rec

        @contextmanager
        def cm():
            try:
                yield rec
            finally:
                rec.finish()

        return cm()


@pytest.fixture
def fake_registry(monkeypatch):
    """把 orchestrator / react 模块的 ToolRegistry 替换为注入版。"""
    FakeRegistry.reset()
    FakeRegistry.register(FakeQueryTool())
    from app.services.agent_tools import RenderChartTool

    FakeRegistry.register(RenderChartTool())
    import app.services.agents.agent_orchestrator as orch_mod
    import app.services.agents.tool_executor as tool_exec_mod

    monkeypatch.setattr(orch_mod, "ToolRegistry", FakeRegistry)
    # 工具执行已抽离到 ToolExecutor（内部使用 tool_executor.ToolRegistry）
    monkeypatch.setattr(tool_exec_mod, "ToolRegistry", FakeRegistry)
    return FakeRegistry


# ======================================================================
# context_utils
# ======================================================================


def test_compact_result_json_preserves_error():
    from app.services.context_utils import compact_result_json

    err = json.dumps({"error": "Binder Error", "hint": "table_ref u1 columns a b"})
    assert compact_result_json(err, 10) == err


def test_compact_result_json_truncates_rows():
    from app.services.context_utils import compact_result_json

    big = json.dumps({"columns": ["c"], "rows": [[i] for i in range(100)]}, ensure_ascii=False)
    small = compact_result_json(big, 500)
    obj = json.loads(small)
    assert len(obj["rows"]) <= 10
    assert obj["rows_total"] == 100 and obj["rows_truncated"] is True
    assert len(small) < len(big)


def test_compact_result_json_short_passthrough():
    from app.services.context_utils import compact_result_json

    short = json.dumps({"a": 1})
    assert compact_result_json(short) == short


def test_compact_metadata_result_not_truncated():
    """元数据类（无 rows，如 list_datasources 全量列名）即使 >1500 也完整保留，不腰斩。"""
    from app.services.context_utils import compact_result_json

    meta = {"datasources": [{
        "name": "ds1",
        "columns": [f"col_{i}" for i in range(80)],
        "fields": [f"col_{i}(VARCHAR)" for i in range(80)],
    }]}
    big = json.dumps(meta, ensure_ascii=False)
    assert len(big) > 1500  # 超过查询结果上限
    assert compact_result_json(big) == big  # 完整保留列名


def test_compact_metadata_huge_still_capped():
    """超大元数据仍受大保底上限封顶（防 pathological，不无限放行）。"""
    from app.services.context_utils import compact_result_json
    from app.config import settings

    huge = json.dumps({"datasources": [{"name": "x", "columns": ["c" * 2000] * 50}]})
    assert len(huge) > settings.RESULT_MAX_META_CHARS
    out = compact_result_json(huge)
    assert "已截断" in out
    assert len(out) <= settings.RESULT_MAX_META_CHARS + 10


def test_compress_history_folds_old_messages():
    from app.services.context_utils import compress_history

    msgs = [{"role": "system", "content": "sys"}]
    for i in range(30):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 100})
    out = compress_history(msgs, keep=20, max_chars=8000)
    assert out[0]["role"] == "system"
    assert any("省略" in m["content"] for m in out)
    assert len(out) <= 22


def test_extract_compressed_digest_collects_marker_summaries():
    from app.services.context_utils import COMPRESSION_MARKER, extract_compressed_digest

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": f"{COMPRESSION_MARKER}【历史记忆】销售额 100，柱状图已生成"},
        {"role": "user", "content": "再分析一下"},
        {"role": "assistant", "content": f"{COMPRESSION_MARKER}新增结论：增长率 20%"},
        {"role": "assistant", "content": "普通回复不算摘要"},
    ]
    digest = extract_compressed_digest(msgs)
    assert "销售额 100" in digest
    assert "增长率 20%" in digest
    assert "普通回复" not in digest


def test_count_rounds_since_marker():
    from app.services.context_utils import COMPRESSION_MARKER, count_rounds_since_marker

    msgs = [
        {"role": "assistant", "content": f"{COMPRESSION_MARKER}old memory"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert count_rounds_since_marker(msgs) == 2


@pytest.mark.asyncio
async def test_smart_compress_chains_after_injected_memory():
    """验证跨轮记忆链路：上一轮记忆以标记注入后，新一轮压缩可继续累积并提取拼接。"""
    from app.services.context_utils import (
        COMPRESSION_MARKER,
        extract_compressed_digest,
        smart_compress_history,
    )

    class _LLM:
        async def complete(self, messages, **kwargs):
            return "新摘要：利润 5000，环比 +8%"

    # 模拟：system + 注入的上一轮记忆 + 3 轮完整对话（含 tool_call/tool 结果）
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": f"{COMPRESSION_MARKER}【历史记忆】旧摘要：销售额 100"},
    ]
    for i in range(3):
        msgs.append({"role": "user", "content": f"question {i}"})
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "query_sql"}}],
        })
        msgs.append({"role": "tool", "content": f'{{"columns": ["c"], "rows": [["{i}", {i}]]}}'})
        msgs.append({"role": "assistant", "content": f"answer {i}"})

    compressed = await smart_compress_history(msgs, _LLM(), min_rounds=3, keep_rounds=1)
    assert compressed is not msgs
    digest = extract_compressed_digest(compressed)
    # 旧记忆保留 + 新摘要追加 → 跨轮记忆连续累积
    assert "旧摘要" in digest
    assert "利润 5000" in digest


# ======================================================================
# stats_analyzer
# ======================================================================


@pytest.mark.asyncio
async def test_stats_analyzer_numeric_and_categorical():
    from app.services.agent_tools import StatsAnalyzerTool

    st = StatsAnalyzerTool()
    res = json.loads(await st.execute(
        columns=["region", "amount", "note"],
        rows=[["A", 10, "x"], ["B", 20, "y"], ["A", 30, None], ["C", "bad", "z"], ["D", 50, "w"]],
    ))
    assert res["row_count"] == 5 and res["column_count"] == 3
    by_col = {s["column"]: s for s in res["columns_stats"]}

    amount = by_col["amount"]
    assert amount["type"] == "numeric"
    assert amount["mean"] == 27.5  # bad 值被过滤，4 个数值样本
    assert amount["median"] == 25.0
    assert amount["min"] == 10.0 and amount["max"] == 50.0
    assert amount["outlier_count"] == 0

    region = by_col["region"]
    assert region["type"] == "categorical" and region["unique_count"] == 4
    assert region["top"][0]["value"] == "A" and region["top"][0]["count"] == 2

    note = by_col["note"]
    assert note["null_count"] == 1


@pytest.mark.asyncio
async def test_stats_analyzer_error_paths():
    from app.services.agent_tools import StatsAnalyzerTool

    st = StatsAnalyzerTool()
    err = json.loads(await st.execute(columns=[], rows=[]))
    assert "error" in err
    err2 = json.loads(await st.execute(columns=["a", "b"], rows=[[1]]))
    assert "error" in err2


# ======================================================================
# 编排器：全流程 + 失败重试 + trace 观测
# ======================================================================


@pytest.mark.asyncio
async def test_orchestrator_full_flow_with_retry_and_trace(fake_registry):
    from app.services.agents.agent_orchestrator import AgentOrchestrator
    import app.services.agents.agent_orchestrator as orch_mod

    capture = TraceCapture()
    orch_mod.get_observer = lambda: capture
    fq = fake_registry.get("query_sql")
    ml = MockOrchestratorLLM()
    orch = AgentOrchestrator(ml, None)

    events = []
    async for ev in orch.execute_task(
        user_msg="查询各区域销售额并生成柱状图",
        history=[],
        user_id="u1",
        available_datasources=[{"id": "ds1", "name": "测试源", "type": "csv",
                                "fields": [{"name": "region"}, {"name": "amount"}]}],
    ):
        events.append(ev)

    # 事件序列：规划(含 selecting status) → analyzing → 查询失败 → 重试成功 →
    # generating → 图表 → reporting → 报告。优化后各阶段会 emit 一次 phase status。
    types = [e["type"] for e in events]
    assert types == ["status", "plan", "status", "tool_call", "tool_result",
                     "tool_call", "tool_result", "status", "tool_call",
                     "tool_result", "chart", "status", "text", "done"], str(types)
    # 各阶段 phase 事件齐备（Task 11）
    phases = [e.get("phase") for e in events if e.get("type") == "status" and "phase" in e]
    assert phases == ["analyzing", "generating", "reporting"], str(phases)
    assert fq.calls == 2  # 1 次失败 + 1 次重试成功（行为保留）
    # 单步为 ReAct 循环，成功后还需一次 LLM 决策以纯文本结束步骤：
    #   查询步骤 3 次（broken/ok/结束文本）+ 图表步骤 2 次（render_chart/结束文本）
    assert ml.tool_llm_calls == 5

    # trace 观测统计
    tr = capture.trace_record
    assert tr is not None
    stats = tr.metadata.get("step_stats") or {}
    # attempts 含步骤结束的纯文本决策轮：查询步骤 2 次工具调用+1 结束；图表 1 次调用+1 结束
    assert stats["1"]["attempts"] == 3 and stats["1"]["retries"] == 2 and stats["1"]["failed"] is False
    assert stats["2"]["attempts"] == 2
    assert tr.metadata.get("total_steps") == 2
    ok_flags = [s.metadata.get("ok") for s in tr.children if s.span_type == "tool"]
    assert ok_flags == [False, True, True], str(ok_flags)


# ======================================================================
# ReAct：循环 + trace 汇总
# ======================================================================


@pytest.mark.asyncio
async def test_react_graph_flow_with_trace(fake_registry):
    from app.services.agents.react_agent import ReactGraphAgent

    trace = TraceRecord(name="react_test")
    ml = MockReactLLM()
    react = ReactGraphAgent(ml, fake_registry.schemas(), trace)
    events = []

    async def emit(ev):
        events.append(ev)

    state = await react.run(
        messages=[
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "查询数据"},
        ],
        user_id="u1",
        db_session=None,
        initial_phase="analyzing",
        emit=emit,
    )

    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text"]
    assert ml.calls == 2
    assert trace.metadata.get("tool_success_count") == 1
    assert trace.metadata.get("iterations") == 2
    assert trace.metadata.get("executed_tool_names") == ["query_sql"]
    assert trace.metadata.get("done_reason") == "final_answer"
    assert len([s for s in trace.children if s.span_type == "tool"]) == 1


# ======================================================================
# 防爆：大工具结果压缩进上下文（error 完整保留）
# ======================================================================


class MiniRegistry:
    """极简工具注册表替身（不依赖共享 FakeRegistry，专用于防爆测试）。"""

    _tools: dict = {}

    @classmethod
    def register(cls, tool) -> None:
        cls._tools[tool.name] = tool

    @classmethod
    def get(cls, name):
        return cls._tools.get(name)


class HugeQueryTool:
    """返回超长结果（>1500 字符）的查询工具，用于验证防爆压缩。"""

    name = "query_sql"

    def schema(self):
        return {"type": "function", "function": {"name": "query_sql", "description": "q",
                "parameters": {"type": "object", "properties": {"datasource_id": {"type": "string"},
                               "sql": {"type": "string"}}, "required": ["datasource_id"]}}}

    async def execute(self, **kwargs):
        return json.dumps({"columns": ["note", "amount"], "rows": [["x" * 40, i] for i in range(200)],
                           "summary": {"row_count": 200}}, ensure_ascii=False)


class ErrorQueryTool:
    """返回错误结果的查询工具，验证错误完整保留。"""

    name = "query_sql"

    def schema(self):
        return {"type": "function", "function": {"name": "query_sql", "description": "q",
                "parameters": {"type": "object", "properties": {"datasource_id": {"type": "string"},
                               "sql": {"type": "string"}}, "required": ["datasource_id"]}}}

    async def execute(self, **kwargs):
        return json.dumps({"error": "Binder Error: column x not found",
                           "hint": "table_ref columns a b"}, ensure_ascii=False)


class CallOnceLLM:
    """防爆测试 Mock：第一轮调工具，第二轮纯文本结束。"""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "tool_call", "name": "query_sql", "id": "c1",
                   "arguments": json.dumps({"datasource_id": "ds1", "sql": "SELECT 1"})}
        else:
            yield {"type": "text", "content": "完成"}


@pytest.mark.asyncio
async def test_react_tool_result_compacted_into_messages(monkeypatch):
    """防爆：大工具结果在 React 路径压缩成摘要进 messages，前端事件仍为全量。"""
    import app.services.agents.tool_executor as tool_exec_mod
    from app.services.agents.react_agent import ReactGraphAgent

    MiniRegistry._tools = {}
    MiniRegistry.register(HugeQueryTool())
    monkeypatch.setattr(tool_exec_mod, "ToolRegistry", MiniRegistry)

    events = []

    async def emit(ev):
        events.append(ev)

    react = ReactGraphAgent(CallOnceLLM(), [HugeQueryTool().schema()], None)
    state = await react.run(
        messages=[{"role": "system", "content": "助手"}, {"role": "user", "content": "查询"}],
        user_id="u1", db_session=None, initial_phase="analyzing", emit=emit,
    )

    # LLM 上下文侧：大结果被压缩（rows 截断 + total/truncated 标记）
    tool_msgs = [m for m in state["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    parsed = json.loads(tool_msgs[0]["content"])
    assert parsed["rows_truncated"] is True
    assert len(parsed["rows"]) <= 10

    # 前端事件侧：保持全量（防爆只作用于 LLM 上下文）
    tr = next(e for e in events if e["type"] == "tool_result")
    event_data = json.loads(tr["result"])
    assert event_data["summary"]["row_count"] == 200
    assert len(event_data["rows"]) == 200


@pytest.mark.asyncio
async def test_react_tool_error_result_kept_full(monkeypatch):
    """防爆边界：error 结果必须完整保留供 LLM 自纠错。"""
    import app.services.agents.tool_executor as tool_exec_mod
    from app.services.agents.react_agent import ReactGraphAgent

    MiniRegistry._tools = {}
    MiniRegistry.register(ErrorQueryTool())
    monkeypatch.setattr(tool_exec_mod, "ToolRegistry", MiniRegistry)

    react = ReactGraphAgent(CallOnceLLM(), [ErrorQueryTool().schema()], None)
    state = await react.run(
        messages=[{"role": "system", "content": "助手"}, {"role": "user", "content": "查询"}],
        user_id="u1", db_session=None, initial_phase="analyzing", emit=None,
    )

    tool_msgs = [m for m in state["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    parsed = json.loads(tool_msgs[0]["content"])
    assert parsed["error"] == "Binder Error: column x not found"
    assert parsed["hint"] == "table_ref columns a b"


# ======================================================================
# 降级观测：Planner 失败 → fallback 计划 + trace 记录 degradation_reason
# ======================================================================


class FailingPlannerLLM:
    """Planner 上游异常 → 编排器走降级计划；Executor 步骤空文本结束。"""

    async def complete(self, messages, **kwargs):
        raise RuntimeError("planner upstream error")

    async def stream_chat_with_tools(self, messages, tools, **kwargs):
        yield {"type": "text", "content": ""}  # 空文本立即结束步骤


@pytest.mark.asyncio
async def test_planner_fallback_records_degradation_trace(fake_registry):
    """Planner 规划失败 → 使用降级计划，并在 trace metadata 记录降级原因。"""
    from app.services.agents.agent_orchestrator import AgentOrchestrator
    import app.services.agents.agent_orchestrator as orch_mod

    capture = TraceCapture()
    orch_mod.get_observer = lambda: capture
    orch = AgentOrchestrator(FailingPlannerLLM(), None)

    events = []
    async for ev in orch.execute_task(
        user_msg="查询销售额",
        history=[],
        user_id="u1",
        available_datasources=[{"id": "ds1", "name": "测试源", "type": "csv",
                                "fields": [{"name": "region"}, {"name": "amount"}]}],
    ):
        events.append(ev)

    tr = capture.trace_record
    assert tr is not None
    # 降级原因被记录进观测 trace（供 Langfuse / 日志统计降级率）
    assert tr.metadata.get("degradation_reason") == "planner_fallback"
    # 降级后仍然产出落地的 plan 事件（用户可见降级后的计划）
    assert any(e.get("type") == "plan" for e in events)


# ======================================================================
# 画布提取辅助函数
# ======================================================================


def test_canvas_extract_helpers():
    from app.api.v1.ai import _extract_chart_config, _extract_sql_block

    assert _extract_sql_block("分析\n```sql\nSELECT * FROM data\n```") == "SELECT * FROM data"
    assert _extract_sql_block("无代码块") == ""

    cfg = _extract_chart_config(
        "```json\n{\"action\": \"apply_chart\", \"chart_type\": \"bar\", \"dimensions\": [\"a\"], \"measures\": [{\"field\": \"b\", \"agg\": \"SUM\"}]}\n```"
    )
    assert cfg is not None and cfg["chart_type"] == "bar"
    assert _extract_chart_config("```json\n{broken\n```") is None


def test_tool_registry_includes_stats_analyzer():
    from app.services.agent_tools import ToolRegistry

    names = [t["function"]["name"] for t in ToolRegistry.schemas()]
    assert "stats_analyzer" in names
    assert "list_fields" in names  # 按需取列工具（表级/列级拆分）
    for tool in (
        "add_chart_block",
        "add_text_block",
        "update_chart_block",
        "remove_block",
        "arrange_layout",
    ):
        assert tool in names
    assert len(names) == 17


def test_list_datasources_returns_table_level_only():
    """list_datasources 拆分为表级：不再内嵌 columns/sample_sql（避免一次性暴露全部列）。"""
    from app.services.agent_tools import ToolRegistry

    desc = next(t for t in ToolRegistry.schemas() if t["function"]["name"] == "list_datasources")
    assert "list_fields(datasource_id)" in desc["function"]["description"]
    # query_sql 描述明确严禁 SELECT *，列名来源指向 list_fields
    qd = next(t for t in ToolRegistry.schemas() if t["function"]["name"] == "query_sql")
    assert "严禁写 SELECT *" in qd["function"]["description"]
    assert "list_fields" in qd["function"]["description"]


@pytest.mark.asyncio
async def test_list_fields_returns_columns_for_datasource():
    """list_fields 按需返回单源完整列名/类型/示例。"""
    from app.services.agent_tools import ListFieldsTool

    import uuid

    class _FakeDS:
        id = uuid.uuid4()
        name = "订单"
        schema_meta = {"fields": [
            {"name": "amount", "data_type": "DOUBLE", "sample": [10.5, 20.0]},
            {"name": "region", "data_type": "VARCHAR"},
        ]}

    class _FakeDB:
        async def execute(self, stmt):
            return self

        def scalar_one_or_none(self):
            return _FakeDS()

    out = json.loads(await ListFieldsTool().execute(
        datasource_id=str(_FakeDS.id), user_id="u1", db_session=_FakeDB(),
    ))
    assert out["columns"] == ["amount", "region"]
    assert out["fields"] == ["amount(DOUBLE)", "region(VARCHAR)"]
    assert out["samples"] == {"amount": [10.5, 20.0]}


async def test_list_fields_unknown_datasource_returns_error():
    from app.services.agent_tools import ListFieldsTool

    class _FakeDB:
        async def execute(self, stmt):
            return self

        def scalar_one_or_none(self):
            return None

    out = json.loads(await ListFieldsTool().execute(
        datasource_id="nope", user_id="u1", db_session=_FakeDB(),
    ))
    assert "error" in out


def test_query_tool_descriptions_draw_clear_boundary():
    """工具描述边界：标准聚合→query_engine 首选，复杂 SQL→query_sql 兜底。"""
    from app.services.agent_tools import ToolRegistry

    desc = {t["function"]["name"]: t["function"]["description"] for t in ToolRegistry.schemas()}
    # query_engine 明确"首选"；query_sql 明确"兜底"并指向对端
    assert "首选" in desc["query_engine"]
    assert "query_sql" in desc["query_engine"]
    assert "query_engine" in desc["query_sql"]  # 兜底说明引用了对端工具


def test_orchestrator_prompt_prefers_query_engine():
    """Planner 示例去 SQL 化：常规聚合示例已改用 query_engine，且工具清单中其列在 query_sql 之前。"""
    from app.services.ai_prompts import ORCHESTRATOR_SYSTEM

    text = str(ORCHESTRATOR_SYSTEM)
    assert '"tool": "query_engine"' in text
    idx_qe = text.find("query_engine（标准聚合首选")
    idx_qd = text.find("query_sql（高级 SQL 兜底")
    assert idx_qe != -1 and idx_qd != -1 and idx_qe < idx_qd


async def test_render_chart_missing_args_friendly_error():
    """render_chart 空参数（LLM 只发工具名）返回引导性错误，而非 Python TypeError。"""
    import json as _json

    from app.services.agent_tools import RenderChartTool

    out = _json.loads(await RenderChartTool().execute())
    assert "error" in out, f"空参数应返回 error，实际 {out}"
    assert "chart_type" in out["error"] and "title" in out["error"]
    assert "hint" in out and "bar" in out["hint"]  # hint 带可用图表类型枚举

    # 部分参数缺失也能指出具体缺项
    out2 = _json.loads(await RenderChartTool().execute(chart_type="bar", title="t"))
    assert "columns" in out2["error"] and "rows" in out2["error"]