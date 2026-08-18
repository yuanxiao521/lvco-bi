# 02 · 整体架构

## 架构图

```mermaid
flowchart TB
    subgraph FE["前端 (React 19 + Vite + TS)"]
        PAGES["页面: AIChat / FreeCanvas / Dashboard / Insights / ..."]
        STORE["Zustand Stores: auth / canvas / notifications / ui"]
        SSE_HOOK["useSSE (fetch+ReadableStream 解析 SSE)"]
        ECH["ECharts / Recharts 渲染"]
    end

    subgraph GW["接入层 (nginx 80)"]
        NG["/ → frontend:3000 | /api/ → backend:8000<br/>/api/v1/ai/ 关闭 proxy_buffering"]
    end

    subgraph API["API 层 (FastAPI /api/v1)"]
        MID["Middleware 链<br/>OperationLog → RequestTiming → CORS"]
        ROUTERS["auth / datasources / canvases / dashboards /<br/>reports / ai / statistics / insights / notifications /<br/>permissions / audit / trash / public"]
        DEPS["api/deps.py 依赖注入工厂<br/>(Repository / Service / 当前用户)"]
    end

    subgraph SVC["服务层 (services/)"]
        AISVC["AIService (聊天/推荐/清洗/洞察/润色)"]
        ORCH["AgentOrchestrator<br/>Planner → SQL → Chart → Report"]
        REACT["单 Agent ReAct 循环<br/>+ 阶段状态机 + 工具注册表"]
        INSIGHT["Insight Engine<br/>auto_discovery / detector / interpreter<br/>report_generator / runner / scheduler"]
        PREF["UserPreferenceService (偏好记忆)"]
        QE["QueryEngine (画布查询/统计)"]
        DATASVC["DatasourceService / ReportService /<br/>CanvasService / NotificationService / ..."]
    end

    subgraph AI["AI 侧"]
        LLM["LLMClient (httpx / OpenAI 兼容<br/>base_url 可切换 provider)"]
        PROMPT["PromptRegistry (prompts/*.yaml)"]
        OBS["observability.py (Langfuse)"]
        GUARD["SQLGuard 三层防护"]
    end

    subgraph DATA["数据层"]
        PG[("PostgreSQL<br/>业务数据 + 洞察表 + 审计")]
        DUCK[("DuckDB<br/>统一分析查询层 lvco_bi.duckdb")]
        REDIS[("Redis 缓存 / SSE")]
        MINIO[("MinIO 报表导出")]
        CSV[("CSV / Excel 上传文件")]
    end

    PAGES --> SSE_HOOK -->|POST /ai/chat/stream| GW
    GW --> API
    API --> DEPS --> SVC
    API --> MID
    SVC --> ORCH
    SVC --> REACT
    SVC --> AISVC
    AISVC --> ORCH
    AISVC --> REACT
    ORCH --> LLM --> PROMPT
    REACT --> LLM
    AISVC --> LLM
    INSIGHT --> LLM
    PREF --> PG
    LLM --> OBS
    REACT --> GUARD --> DUCK
    ORCH --> DUCK
    QE --> DUCK
    DATASVC --> DUCK
    DUCK -->|ATTACH| PG
    DATASVC --> CSV
    SVC --> REDIS
    SVC --> MINIO
    API --> PG
    ECH --> PAGES
```

## 分层职责

| 层 | 目录 | 职责 |
|----|------|------|
| API 层 | `app/api/v1/` | 路由定义、请求/响应校验（Pydantic）、限流、SSE 流式封装 |
| 装配层 | `app/api/deps.py` | 依赖注入：JWT 用户 + Repository/Service 工厂（**唯一装配点**） |
| 服务层 | `app/services/` | 业务逻辑：AI 全链路、Agent、洞察引擎、查询引擎、数据源、报表等 |
| 仓储层 | `app/repositories/` | 数据访问：Protocol 接口 + SQLAlchemy 实现 + 缓存实现（Redis/内存/降级） |
| 模型层 | `app/models/` | SQLAlchemy ORM 模型 + 枚举（15 张表） |
| 核心层 | `app/core/` | 横切组件：DB 引擎、DuckDB 客户端、安全、中间件、SSE、限流、操作日志 |
| 连接器层 | `app/connectors/` | 数据源抽象与实现（CSV/Excel/MySQL/PostgreSQL） |
| 前端 | `frontend/src/` | React 页面、Zustand、SSE hooks、图表组件 |

## 核心数据流

### 1. 自然语言 → 图表（AI 主链路）

```mermaid
sequenceDiagram
    participant U as 用户
    participant F as 前端 AIChat
    participant API as /ai/chat/stream
    participant SVC as AIService.agent_stream
    participant LLM as LLMClient
    participant DUCK as DuckDB

    U->>F: 提问（可选已选数据源）
    F->>API: POST /ai/chat/stream (SSE)
    API->>SVC: 会话/消息持久化 + 安全闸门 SQLGuard(L1+L2)
    alt 多 Agent 模式
        SVC->>LLM: PlannerAgent 生成执行计划
        SVC->>LLM: SQLAgent 生成 SQL
        SVC->>DUCK: 执行 SQL（table_ref + 字段白名单）
        SVC->>LLM: ChartAgent 生成 ECharts option
        SVC->>LLM: 生成 Markdown 分析报告
    else 单 Agent ReAct 模式
        loop 最多6轮, 阶段状态机 SELECTING→ANALYZING→GENERATING→REPORTING
            SVC->>LLM: stream_chat_with_tools(按阶段暴露工具)
            SVC->>SVC: 执行 list_datasources / query_datasource / render_chart
        end
    end
    SVC-->>API: text 增量 / tool_call / chart / done (SSE)
    API-->>F: 流式文本 + 批量图表 option
    F->>F: 前端 ECharts 渲染 + 报告展示
```

### 2. 洞察引擎定时流程

```mermaid
flowchart LR
    A["auto_discovery<br/>扫描 PG 数据源<br/>启发式打分"] --> B["InsightSuggestion<br/>(pending)"]
    B --> C["用户接受 → 建 InsightRule"]
    C --> D["Scheduler<br/>daily/weekly<br/>CronTrigger + PG advisory lock"]
    D --> E["Runner: 查时间序列"]
    E --> F["Detector<br/>z-score / WoW / YoY / MA"]
    F --> G["Interpreter (LLM 解读)"]
    G --> H["InsightRecord +<br/>ReportGenerator(ai_insight) +<br/>通知推送"]
```

### 3. 数据源查询统一入口

```mermaid
flowchart LR
    A["数据源注册<br/>upload / connect"] --> B["Sync: 灌入 DuckDB 或 ATTACH"]
    B --> C["schema_meta 生成<br/>(字段分类 + 抽样值)"]
    C --> D["QueryEngine / 统计 / Agent 工具<br/>统一走 DuckDB 执行"]
    D --> E["SQLGuard L3 校验<br/>SELECT-only + LIMIT"]
```

## 关键设计决策（ADR 摘要）

| 决策 | 选择 | 原因 |
|------|------|------|
| 分析引擎 | DuckDB 而非直接查 PG | 统一多数据源查询、列式分析性能、无额外服务 |
| LLM 接入 | 裸 httpx 走 OpenAI 兼容协议 | 一套代码兼容 DeepSeek/通义/OpenAI 等任意 provider |
| 数据库访问 | SQLAlchemy 2 异步 + 仓储模式 | 高并发、可测试（Protocol 支持 mock）、UoW 统一事务 |
| Agent 架构 | 编排 + ReAct 双模式 | 简单任务快、复杂任务结构化；Feature Flag 可灰度 |
| Prompt 管理 | YAML 外部化 + 注册中心 | 产品/运营可迭代提示词，无需改代码重启 |
| 可观测 | Langfuse 四层埋点 | 端到端追踪 LLM 调用成本与质量，未配置零侵入 |
| PDF 导出 | 子进程 Playwright | 规避 Windows ProactorEventLoop 限制 |

## 模块依赖关系（后端 services 层）

```mermaid
flowchart TD
    AI["ai_service.py"] --> LLM["llm_client.py"]
    AI --> REG["prompt_registry.py"]
    AI --> PREF["user_preference_service.py"]
    ORCH["agent_orchestrator.py"] --> PL["planner_agent.py"]
    ORCH --> SQL["sql_agent.py"] --> DUCK["duckdb_client.py"]
    ORCH --> CH["chart_agent.py"] --> LLM
    ORCH --> GUARD["sql_guard.py"]
    TOOLS["agent_tools.py"] --> GUARD
    TOOLS --> DUCK
    INS["insight_engine/"] --> DUCK
    INS --> LLM
    QE["query_engine.py"] --> DUCK
    OBS["observability.py"] --> LANGFUSE["Langfuse SDK(可选)"]
    LLM --> OBS
```
