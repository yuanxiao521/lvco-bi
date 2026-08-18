# 04 · 后端目录结构

> 根目录：`backend/`（FastAPI 单体应用）

## 目录树（关键文件）

```
backend/
├── app/
│   ├── main.py                  # 应用入口：中间件、路由挂载、生命周期、异常处理器
│   ├── config.py                # pydantic-settings 集中配置 + 任务分级模型路由
│   ├── api/
│   │   ├── deps.py              # 依赖注入中枢（JWT 用户 + Repository/Service 工厂）
│   │   └── v1/
│   │       ├── router.py        # 聚合所有 v1 子路由（前缀 /api/v1）
│   │       ├── auth.py          # 认证：登录/注册/刷新/登出/改密/改资料
│   │       ├── datasources.py   # 数据源：上传/连接/同步/预览/AI 清洗
│   │       ├── canvases.py      # 画布：CRUD/查询/图表配置/PDF 导出/AI 推荐
│   │       ├── dashboards.py    # 仪表盘：CRUD/图表/数据/刷新/分享
│   │       ├── reports.py       # 报表：CRUD/状态/分享/PDF 导出(MinIO)
│   │       ├── ai.py            # AI：会话/消息/Agent 流式对话/清洗/洞察/润色
│   │       ├── insights.py      # 智能洞察：规则/手动运行/发现/建议
│   │       ├── statistics.py    # 统计分析：描述/相关/排名/对比/汇总
│   │       ├── notifications.py # 通知：CRUD + SSE 实时流
│   │       ├── permissions.py   # 权限管理：用户列表/改角色
│   │       ├── audit.py         # 审计：日志查询/汇总/CSV 导出
│   │       ├── trash.py         # 回收站：列表/恢复/彻底删除
│   │       └── public.py        # 免认证分享视图
│   ├── core/                    # 横切组件
│   │   ├── database.py          # SQLAlchemy 异步引擎/会话工厂
│   │   ├── duckdb_client.py     # DuckDB 单例客户端（统一查询执行）
│   │   ├── security.py          # bcrypt + JWT 签发/解码
│   │   ├── middleware.py        # 请求计时 + 操作审计中间件
│   │   ├── sse.py               # 用户级 SSE 连接池（SSEManager）
│   │   ├── limiter.py           # slowapi 限流器
│   │   └── operation_log.py     # 审计标签推断（method+path → resource/action）
│   ├── models/                  # SQLAlchemy ORM（16 文件，15 张表，见 06）
│   ├── repositories/            # 仓储层（Protocol + 实现 + 缓存，见 08）
│   ├── connectors/              # 数据源连接器（base/csv/excel/mysql/postgres）
│   ├── schemas/                 # Pydantic 请求/响应模型（insight/query）
│   ├── services/                # 服务层（见下）
│   └── utils/crypto.py          # AES 加解密（数据源密码）
├── prompts/                     # LLM 提示词 YAML（9 个，见 10）
│   ├── agent_system.yaml
│   ├── chat_system.yaml
│   ├── recommend_system.yaml
│   ├── clean_system.yaml
│   ├── insights_system.yaml
│   ├── polish_system.yaml
│   ├── chat_data_system.yaml
│   ├── canvas_system.yaml
│   └── insight_report_system.yaml
├── alembic/                     # 迁移（versions/ 13 个迁移，见 09）
│   ├── env.py                   # 使用 settings.DATABASE_URL 覆盖配置
│   └── versions/
├── scripts/                     # 工具脚本（见 09）
│   ├── generate_mock_data.py    # 生成 6 个模拟 CSV
│   ├── upload_mock_data.py      # 批量上传为数据源
│   ├── pdf_worker.py            # Playwright PDF 渲染子进程
│   ├── quick_check.py           # 数据源自检
│   ├── register_pg_datasources.py / setup_pg_datasource.py  # PG 数据源注册
│   └── test_duckdb_attach.py    # DuckDB ATTACH 验证
├── tests/                       # 测试（insight_engine 单测、auto 测试等）
├── mock_data/                   # 6 个业务模拟 CSV
├── requirements.txt             # 锁定依赖
├── pyproject.toml               # 项目元数据 + dev 依赖
├── alembic.ini                  # Alembic 配置
├── start_backend.ps1            # Windows 启动脚本（uvicorn 8000）
├── Dockerfile                   # 后端镜像
└── .env.example / .env.backup   # 环境变量模板
```

## 服务层（`app/services/`）模块职责总览

### AI 与 Agent
| 文件 | 职责 |
|------|------|
| `llm_client.py` | LLM 客户端：complete / stream_chat / stream_chat_with_tools（OpenAI 兼容） |
| `ai_service.py` | AI 主服务：聊天流、图表推荐、清洗建议、洞察、润色、`agent_stream` 双模式分流 |
| `ai_prompts.py` | 旧常量 → PromptRegistry 的兼容壳（含 9 个 Fallback 文本） |
| `prompt_registry.py` | Prompt 注册中心：YAML 加载、版本、`reload()` 热更新 |
| `agent_tools.py` | 工具注册表 + 阶段状态机 + 3 个工具 + 多度量 ECharts option 构建 |
| `agents/base_agent.py` | Agent 抽象基类（AgentResult / execute / stream_execute / 计时） |
| `agents/planner_agent.py` | 规划 Agent（生成执行计划 JSON） |
| `agents/sql_agent.py` | SQL Agent（生成并执行 SQL） |
| `agents/chart_agent.py` | 图表 Agent（生成 ECharts option） |
| `agents/agent_orchestrator.py` | 多 Agent 编排器（Planner→SQL→Chart→Report 四步流水线） |

### 查询 / 安全 / 质量
| 文件 | 职责 |
|------|------|
| `query_engine.py` | 画布查询统一入口：SQL 构建（参数化）、缓存、字段校验 |
| `sql_guard.py` | SQL 三层安全防护（L1 净化 / L2 意图 / L3 输出控制） |
| `data_quality.py` | 6 类数据质量检测（null/outlier IQR/Z/类型/重复/格式） |
| `chart_renderer.py` | matplotlib 渲染（PDF/报告导出用，与前端 ECharts 无关） |

### 洞察引擎（`insight_engine/`，见 12）
`auto_discovery.py`（自动发现）· `detector.py`（异常检测）· `interpreter.py`（LLM 解读）· `report_generator.py`（洞察报告）· `runner.py`（执行器）· `scheduler.py`（调度）

### 业务服务
| 文件 | 职责 |
|------|------|
| `auth_service.py` | 注册/登录/令牌（路由调用） |
| `datasource_service.py` | 数据源注册、同步、schema 提取 |
| `canvas_service.py` / `dashboard_service.py` | 画布/仪表盘业务 |
| `report_service.py` | 报表 CRUD、PDF 导出（MinIO 优先） |
| `cache_service.py` | 缓存服务（SimpleCache + Redis 双实现，单例 `cache`） |
| `storage_service.py` | MinIO 对象存储封装 |
| `notification_service.py` | 通知写库 + SSE 推送双通道 |
| `user_preference_service.py` | 用户偏好记忆（显式/隐式/衰减/注入） |
| `observability.py` | Langfuse 可观测封装（Trace/Span/Generation/Tool） |

## 关键入口文件

- **`app/main.py`**：`FastAPI(title="Lvco BI")`，中间件顺序 `OperationLog → RequestTiming → CORS`，注册 `rate_limit_handler`（429）与兜底异常处理器（500），startup 时 ping Redis（失败降级内存缓存）。
- **`app/config.py`**：`Settings(BaseSettings)`，关键派生属性 `is_ai_configured` / `is_langfuse_configured` / `cors_origins_list`，方法 `model_for_task(task_type)`。
- **`app/api/deps.py`**：`get_current_user`（HTTPBearer → JWT → 查库）、全套 `get_xxx_repository/service` 工厂、`get_cache_repository`（Fallback 单例）。
