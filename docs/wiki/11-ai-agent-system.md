# 11 · AI Agent 系统（核心亮点）

> 涉及文件：`services/ai_service.py`（agent_stream 入口）、`services/agent_tools.py`、`services/agents/*`（base/planner/sql/chart/agent_orchestrator）。
> 入口路由：`POST /api/v1/ai/chat/stream`（SSE）。

## 双模式架构

```
用户提问 → 安全闸门 SQLGuard(L1+L2)
  ├─ 多 Agent 模式：AgentOrchestrator（Planner → SQL → Chart → Report 四步流水线）
  └─ 单 Agent ReAct 模式：工具调用循环 + 阶段状态机（默认路径）
多 Agent 异常 → 自动降级单 Agent ReAct
```

**分流条件**（`ai_service.agent_stream`）：`AGENT_ORCHESTRATOR_ENABLED=True` 且 `initial_phase=="selecting"` 且 `len(user_msg)>20` 且不以「列出/有哪些」开头 → 多 Agent；否则单 Agent ReAct。

## 单 Agent ReAct 循环

### 阶段状态机（`agent_tools.py`）

```python
class ConversationPhase(str, Enum):
    SELECTING   # 只暴露 list_datasources
    ANALYZING   # query_datasource + list_datasources（失败自纠错）
    GENERATING  # 只暴露 render_chart
    REPORTING   # 无工具
```

阶段自动流转：执行过 `query_datasource` → ANALYZING→GENERATING；执行过 `render_chart` → GENERATING→REPORTING。

### 循环细节

- 上限 `MAX_ITERATIONS=6`；连续失败熔断 `MAX_CONSECUTIVE_FAILURES=5`。
- 每轮：按阶段过滤工具 → `llm.stream_chat_with_tools` 流式文本/tool_call → 执行工具 → 结果回填 messages(role=tool) + 注入 follow-up 指令（查询失败自纠错 hint / 成功后强制先 render_chart 再输出 Markdown 报告）。
- 终答条件：有文本且无 tool_call → `yield done`。
- 事件清洗（API 层）：`_strip_and_normalize` 剥离 ```sql/```json 代码块；`_filter_status_text` 过滤"正在查询…/✅"等状态旁白；chart 事件按 `chart_type::option` 去重，done 时一次性携带全部 charts。

## 工具系统（`agent_tools.py`）

```python
class BaseTool(ABC):          # name / description / schema() / execute(**kwargs) -> JSON str
class ToolRegistry:           # register / get / schemas() 类方法注册表
get_tools_for_phase(phase, all_schemas) -> list[dict]   # 按阶段过滤 OpenAI function schema
```

| 工具 | 作用 | 关键行为 |
|------|------|---------|
| `ListDatasourcesTool` | 按 user_id 列出数据源 | 构造 `columns`/`fields`/`table_ref`（`"schema"."public"."table"` 或 `"schema"."data"`）/`sample_sql`/`tip` |
| `QueryDatasourceTool` | 执行查询 | 先 `sql_guard.full_check("", sql)`（L3）→ 校验归属 → PG/MySQL 解密 ATTACH → DuckDB fetchall → 正则解析列名 → 返回前 50 行；失败返回**自纠错 hint**（正确 table_ref + 真实列名） |
| `RenderChartTool` | 生成 ECharts option | 13 种类型；columns≥3 且为 bar/line/area/stacked/grouped/horizontal 时走 `_build_multi_measure_option`（**双 Y 轴 + PowerBI 风格图例，镜像前端 echartsUtils**）；kpi_card 用 gauge |

## 多 Agent 编排（`agents/agent_orchestrator.py`）

顺序流水线（非并行协商），三个子 Agent 共享同一 LLMClient 实例：

1. **PlannerAgent**：`execute(user_msg, history, available_datasources)` → LLM 输出 `{primary_intent, steps[{step_id, action, target, parameters, depends_on}], expected_output}`；解析失败回退默认规划（list_datasources 一步）。事件：`planner_start`/`planner_plan`/`planner_error`。
2. **SQLAgent**：`execute(user_msg, datasource_info, query_context)` → 仅对**用户输入**做 L1+L2 → 生成 SQL（```sql 块提取）→ 执行 DuckDB（⚠️ 引用不存在的 `execute_query`，实际会失败被捕获为查询失败事件，应改 `fetchall`；且生成的 SQL 未过 L3）。
3. **ChartAgent**：`execute(query_result, user_intent, chart_preferences)` → LLM 返回 `{chart_type, option}`。
4. **报告生成**：`_generate_report` 用「用户问题 + 执行计划 + 查询结果」生成 Markdown 报告 → `yield text` → `yield done`。

事件流：`status("正在分析您的需求...")` → `plan` → `sql_result{sql, data, row_count}` → `chart{chart_type, option}` → `text(报告)` → `done`；任一查询失败 `yield error` 终止；图表失败 `yield warning`（不阻断）。

## HITL（人机确认，服务层已定义）

单 Agent 模式在 `agent_stream` 中产出两类确认事件（**API 路由层尚未转发到前端**）：

- `confirm_sql`：query_datasource 成功后 `{sql, result_preview(前5行), message:"SQL 查询已执行，是否接受这个结果？"}`
- `confirm_chart`：render_chart 成功后 `{chart_type, options, message:"图表已生成，是否接受这个图表？"}`

多 Agent 模式无确认事件，全自动执行。

## 关键类/函数签名一览

| 类/函数 | 说明 |
|---------|------|
| `AgentResult(success, data=None, error=None, metadata, execution_time_ms=0)` | Agent 输出统一载体 |
| `BaseAgent(name)` | 抽象基类：`execute(**kwargs)` / `stream_execute(**kwargs)` / `track_execution()` 计时 |
| `PlannerAgent` / `SQLAgent` / `ChartAgent` | 三个子 Agent 实现 |
| `AgentOrchestrator(llm, db_session)` | `execute_task(...) -> AsyncIterator[dict]` 四步流水线 |
| `AIService.agent_stream(user_id, user_msg, history, db_session, initial_phase="selecting")` | 双模式分流入口 |

## 降级策略

- 多 Agent 抛异常 → 输出「多 Agent 编排失败，自动回退到单 Agent 模式」继续走 ReAct。
- 单 Agent 查询连续失败 5 次 → 熔断终止。
