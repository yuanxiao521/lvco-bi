# Lvco BI · Code Wiki

> 项目根目录：`e:/BI/LvcoBI/lvco-bi`
> Wiki 版本：**v3.0**（对齐当前代码：多 Agent 系统、Insight Engine、Prompt Registry、用户偏好记忆、Langfuse 可观测）
> 适用代码版本：`backend/requirements.txt` 与 `frontend/package.json` 当前依赖快照

## 一句话定位

**Lvco BI** 是一个 AI 原生的数据分析与可视化平台：用户用自然语言提问，系统通过**多 Agent 编排 / ReAct 循环**自动完成「选数据源 → 生成 SQL → 查询 → 生成图表 → 输出分析报告」全流程，并提供画布、仪表盘、智能洞察、报表导出等完整的 BI 产品能力。

## Wiki 目录

| # | 文件 | 说明 |
|---|------|------|
| — | [README.md](./README.md) | 本索引：目录、阅读建议、关键设计原则 |
| 01 | [01-overview.md](./01-overview.md) | 项目定位、核心能力、功能矩阵 |
| 02 | [02-architecture.md](./02-architecture.md) | **整体架构图**、分层职责、核心数据流 |
| 03 | [03-tech-stack.md](./03-tech-stack.md) | 前/后端技术栈与依赖清单 |
| 04 | [04-backend-structure.md](./04-backend-structure.md) | 后端目录结构全览 |
| 05 | [05-backend-api.md](./05-backend-api.md) | REST API 路由全清单（`/api/v1/*`） |
| 06 | [06-backend-models.md](./06-backend-models.md) | ORM 数据模型 + **E-R 图** |
| 07 | [07-backend-core.md](./07-backend-core.md) | Core 层：DB / DuckDB / Security / Middleware / SSE / Limiter |
| 08 | [08-backend-repositories-cache.md](./08-backend-repositories-cache.md) | 仓储模式（Protocol + SQLAlchemy + UoW）与三层缓存 |
| 09 | [09-backend-connectors-migrations.md](./09-backend-connectors-migrations.md) | 数据源连接器、Alembic 迁移历史、脚本工具 |
| 10 | [10-ai-llm-prompt.md](./10-ai-llm-prompt.md) | **LLM 客户端、任务分级模型路由、Prompt 注册中心** |
| 11 | [11-ai-agent-system.md](./11-ai-agent-system.md) | **多 Agent 编排 + 单 Agent ReAct + 工具系统 + HITL**（核心亮点） |
| 12 | [12-ai-insight-engine.md](./12-ai-insight-engine.md) | **智能洞察引擎**：自动发现 / 异常检测 / LLM 解读 / 报告 / 调度 |
| 13 | [13-ai-preference-guard-query.md](./13-ai-preference-guard-query.md) | 用户偏好记忆 + SQL 三层安全防护 + 统一查询引擎 |
| 14 | [14-frontend.md](./14-frontend.md) | 前端结构、页面路由、SSE 流式对话、图表渲染 |
| 15 | [15-running-deployment.md](./15-running-deployment.md) | 本地开发运行 + Docker 生产部署 |
| 16 | [16-resume-overview.md](./16-resume-overview.md) | 简历用项目总览概述（突出 AI 应用开发能力） |

## 阅读建议

- **第一次进入**：按 01 → 02 → 06 → 10 → 11 → 12 顺序通读，建立「AI 全链路」全貌；
- **接手后端**：重点读 04 / 05 / 06 / 07 / 08 / 09；
- **想了解 AI 能力**：重点读 10 / 11 / 12 / 13（Agent、Prompt、偏好、安全、洞察）；
- **接手前端**：读 14；
- **上线与排障**：15 + 07 的 Middleware / SSE / Limiter。

## 关键设计原则

1. **DuckDB 统一查询层**：CSV / Excel 走 `read_csv_auto`，PostgreSQL / MySQL 通过 `ATTACH` 拉进 DuckDB，一份 `query_engine` 覆盖所有数据源；schema 名按 `db_name/数据源名_数据源ID前8位` 隔离，避免跨库串数据。
2. **LLM 调用统一收敛**：所有 LLM 调用集中在 `services/llm_client.py`（裸 httpx 走 OpenAI 兼容协议），业务层只表达 prompt 与工具声明，不直接发请求。
3. **任务分级模型路由**：`config.Settings.model_for_task()` 按任务复杂度（simple/complex）路由到不同模型，控制成本。
4. **双模式 Agent 引擎**：`/ai/chat/stream` 走「多 Agent 编排（Planner→SQL→Chart→Report）」，Feature Flag 关闭或失败时自动降级「单 Agent ReAct 循环」。
5. **Prompt 外部化管理**：`backend/prompts/*.yaml`（9 个模板）+ `PromptRegistry` 单例加载，支持 `reload()` 热更新；`ai_prompts.py` 为向后兼容层。
6. **SQL 三层安全防护**：L1 输入净化 → L2 意图分析 → L3 SQL 输出控制（黑名单 + 自动 LIMIT），AI 生成 SQL 一律只允许 SELECT。
7. **用户偏好记忆**：`user_preferences` 表记录图表类型/配色/维度等偏好（显式 1.0 / 隐式 0.5 起），带 30 天衰减，注入图表推荐 prompt。
8. **通知双通道**：`NotificationService` 既写 `notifications` 表又调 `sse_manager.publish`，前端 `useNotificationStream` 用 EventSource 接收。
9. **操作可审计 + LLM 可观测双轨**：`OperationLogMiddleware` 后台异步写 `operation_logs`；`observability.py` 对 Agent/LLM 调用做 Langfuse Trace/Span/Generation 埋点，未配置均自动降级不阻塞业务。
10. **单体 API 优先**，无状态水平扩展，软删除统一回收（回收站），SSE 走 nginx 关闭缓冲。

## 技术栈速览

- **后端**：Python 3.12 · FastAPI · SQLAlchemy 2 (async) · PostgreSQL · DuckDB · Redis · MinIO · OpenAI 兼容 LLM · Langfuse
- **前端**：React 19 · TypeScript · Vite · Tailwind 4 · Zustand · ECharts / Recharts · axios
- **部署**：Docker Compose（nginx + frontend + backend + postgres + redis + minio）
