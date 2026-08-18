# 16 · 简历总览概述（突出 AI 应用开发能力）

> 以下内容可直接用于简历「项目经历」板块。写法上按「AI 能力亮点 → 工程化能力 → 量化成果」组织，面试时可展开为 STAR 故事。

---

## 项目名称

**Lvco BI — AI 原生数据分析与可视化平台（Text-to-BI）** | 独立开发 | FastAPI + React + LLM Agent

### 项目一句话

基于 **FastAPI + React + LLM Agent** 自研的 AI 原生 BI 平台：用户用自然语言提问，系统通过**多 Agent 编排（Planner→SQL→Chart→Report）**自动完成「选数据源 → 生成并安全校验 SQL → 查询 → 生成图表 → 输出分析报告」全流程，并内置智能洞察、用户偏好记忆、审计与可观测等企业级能力。

### 核心技术栈

- **后端**：Python · FastAPI · SQLAlchemy 2（异步）· PostgreSQL · DuckDB · Redis · MinIO
- **前端**：React 19 · TypeScript · Vite · Zustand · ECharts/Recharts · SSE 流式
- **AI**：OpenAI 兼容 LLM（裸 httpx 协议层接入，可切换 DeepSeek 等任意 Provider）· 多 Agent 编排 · ReAct · Function Calling · Prompt 工程 · Langfuse 可观测

### AI 能力亮点（面试重点）

1. **自研双模式 Agent 引擎**
   - 设计多 Agent 编排器：Planner（需求拆解为执行计划）→ SQLAgent（生成并执行 SQL）→ ChartAgent（生成 ECharts 配置）→ 报告生成，事件以 SSE 流式回传；
   - 单 Agent 模式实现 ReAct 循环 + 阶段状态机（选源→分析→出图→报告），工具按阶段暴露、连续失败熔断、失败自动纠错（工具返回正确表名/列名 hint 引导 LLM 重试）；
   - 双模式用 Feature Flag 灰度，编排失败自动降级单 Agent，保证可用性。

2. **LLM 工程化落地**
   - 统一 LLM 客户端（complete / 流式 / 带工具调用三种能力），任务分级模型路由（简单任务 vs 复杂任务用不同模型）控制成本；
   - Prompt 外部化管理：9 个 YAML 模板 + 注册中心 + 热更新，业务代码零硬编码；
   - Langfuse 全链路可观测（Trace/Span/Generation/Tool 四层埋点），未配置零侵入降级。

3. **AI 安全防线**
   - 自研 SQL 三层防护：输入净化（prompt 注入/危险 SQL 正则）→ 意图分析（写操作/提权/脱库关键词）→ SQL 输出控制（SELECT-only 白名单 + 自动 LIMIT），LLM 生成的 SQL 一律过闸。

4. **智能洞察引擎**
   - 数据源自动发现（启发式打分识别可监控表）→ 时间序列异常检测（z-score / 环比 / 同比 / 移动平均四类纯统计算法）→ LLM 自然语言解读 → 自动生成洞察报告并推送通知，支持 cron 定时调度 + PG 分布式锁防重复执行。

5. **用户偏好记忆（个性化）**
   - 设计偏好记忆系统：显式/隐式偏好采集、强度累积、30 天时间衰减，注入图表推荐 prompt，让推荐结果随使用越来越准。

6. **工程化能力**
   - 分层架构（API/Service/Repository/UoW/Connector），仓储模式 + Protocol 接口支持 mock 测试；
   - DuckDB 统一查询层，一份查询引擎覆盖 CSV/Excel/PostgreSQL/MySQL 四类数据源，schema 级隔离防串数据；
   - 15 张表 PostgreSQL 建模 + 13 个 Alembic 迁移版本管理；JWT 认证、角色权限、操作审计、限流、SSE 通知、软删除回收站完整闭环。

### 量化成果（示例口径，可按实际情况调整）

- 覆盖 20+ 个 REST API 端点、13 个数据库迁移、9 个 Prompt 模板、6 类数据质量检测、4 类异常检测算法；
- AI 全链路（提问→图表→报告）一次对话完成，多 Agent 与单 Agent 双引擎互为兜底；
- 全流程数据可审计 + LLM 调用可追踪（成本/质量），适合企业级落地。

### 面试可讲的故事

- **"怎么让 LLM 少出错？"** → SQL 三层防护 + 工具失败自纠错 hint + 熔断机制；
- **"怎么控制成本？"** → 任务分级模型路由 + 图表去重 + 历史摘要注入减少 token；
- **"Agent 怎么设计？"** → 编排 vs ReAct 双模式 + 阶段状态机 + 工具注册表；
- **"怎么保证不串数据？"** → DuckDB schema 按用户+数据源隔离；
- **"Prompt 怎么迭代？"** → YAML 外部化 + 注册中心热更新，运营可改不改代码。
