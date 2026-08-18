# 01 · 项目定位与核心能力

## 项目定位

**Lvco BI** 是一个 **AI 原生的自助式数据分析与可视化平台（Text-to-BI）**。核心价值主张：

> 用户无需掌握 SQL 和图表知识，用自然语言提问（如"分析上季度各品类销售额并画图"），系统自动完成「选数据源 → 生成并校验 SQL → 查询 → 生成图表 → 输出分析报告」全流程，并支持定时智能洞察、报表导出等企业级 BI 能力。

项目以课程设计 / 毕设级工程完整度交付，覆盖认证、权限、数据源管理、可视化画布、仪表盘、报表、审计、通知、回收站等完整产品闭环。

## 核心能力矩阵

| 能力域 | 具体功能 | 关键模块 |
|--------|---------|---------|
| **自然语言 → 图表** | 多 Agent 编排 / 单 Agent ReAct 完成「选源→SQL→查询→图表→报告」 | `services/agents/*`、`services/ai_service.py` |
| **AI 对话** | 流式 SSE 对话、会话管理、自动标题、历史图表去重 | `api/v1/ai.py`、`AIChat` 页面 |
| **智能洞察** | 数据源自动发现、时间序列异常检测（z-score/WoW/YoY/MA）、LLM 解读、洞察报告、定时调度 | `services/insight_engine/*` |
| **图表推荐** | LLM 推荐 + 规则兜底双路径，注入用户偏好，14 种图表类型 | `ai_service.recommend_charts` |
| **数据清洗** | AI 生成清洗建议（缺失/离群/重复/类型/格式），执行 DELETE/UPDATE | `api/v1/ai.py`、`data_quality.py` |
| **数据分析** | 描述统计、相关性矩阵、排名、同比/环比、汇总（DuckDB 窗口函数） | `api/v1/statistics.py` |
| **数据源管理** | CSV/Excel 上传、PostgreSQL/MySQL 直连、schema 自动同步、预览、AI 质量分析 | `connectors/*`、`datasource_service.py` |
| **可视化** | 自由画布（拖拽/网格对齐/内嵌 AI 助手）、仪表盘、14 种 ECharts 图表、多度量双 Y 轴 | `frontend`、`chart_config` |
| **报表导出** | 画布存为报表、PDF 导出（Playwright 子进程 / MinIO 预签名 URL）、公开分享 | `reports.py`、`pdf_worker.py` |
| **用户偏好记忆** | 显式/隐式偏好记录、强度累积、30 天衰减、注入推荐 | `user_preference_service.py` |
| **平台工程** | JWT 认证、角色权限、操作审计、SSE 通知、限流、软删除回收站 | `core/*`、`api/v1/*` |

## 亮点特性（工程视角）

1. **双模式 Agent 引擎**：`AGENT_ORCHESTRATOR_ENABLED` 开关控制多 Agent 编排；单 Agent ReAct 有阶段状态机（SELECTING→ANALYZING→GENERATING→REPORTING）、工具按阶段暴露、失败自动纠错（工具返回正确 table_ref 与真实列名 hint）、连续失败熔断。
2. **Prompt 外部化管理**：prompts YAML + 注册中心 + 热更新，业务代码不硬编码 prompt。
3. **SQL 安全三层防护**：即使 LLM 被诱导，生成 SQL 也会被 SELECT-only 白名单拦截。
4. **LLM 成本控制**：任务分级模型路由 + 图表去重 + 历史摘要注入，减少 token 消耗。
5. **全链路可观测**：Langfuse Trace/Span/Generation/Tool 四层埋点，未配置自动降级。
6. **洞察引擎自闭环**：自动发现→建议→一键建规则→定时执行→异常检测→LLM 解读→生成 Report→推送通知。

## 版本范围

- 当前 Wiki 对应代码状态：**多 Agent 编排系统 + Insight Engine + Prompt Registry + 用户偏好记忆 + Langfuse 可观测均已实现**。
- 已知未完成/占位项：
  - `HITL（人机确认）事件`（`confirm_sql`/`confirm_chart`）在服务层已产出，但 API 路由层尚未转发给前端；
  - 偏好**写入侧**（`record_implicit/explicit_preference`）逻辑完备但尚未在 API 层接线（读取侧已生效）；
  - 多 Agent 编排器 SQL 路径引用 `duckdb_client.execute_query`（实际不存在该方法，运行时会失败并降级为查询失败事件，需改为 `fetchall`）；
  - 洞察规则定时执行依赖 APScheduler，调度器为轻量实现（未启动时 noop）。

## 主要用户角色

| 角色 | 权限 |
|------|------|
| `admin` | 全部功能 + 权限管理 + 回收站彻底删除 |
| `editor`（默认） | 常规业务功能 |
| `viewer` | 只读（审计日志不区分读写详情） |
