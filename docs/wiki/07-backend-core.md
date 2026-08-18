# 07 · Core 层（横切组件）

> 目录：`app/core/`。这些组件被 API / Service / Agent 各层共用。

## `database.py` — 数据库引擎与会话

```python
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)  # asyncpg
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db() -> AsyncSession:  # FastAPI 依赖：正常 commit / 异常 rollback
```

## `duckdb_client.py` — DuckDB 单例客户端（统一查询执行）

- 双锁延迟连接（单例锁 + 连接锁），数据库文件 `{DUCKDB_DATA_DIR}/lvco_bi.duckdb`，`memory_limit` 2GB。
- 启动装载扩展：`spatial`（Excel 读取）、`postgres_scanner`（PostgreSQL ATTACH）。
- 核心方法：`execute(sql, params)` / `fetchall(sql, params)` / `fetchdf(sql, params)`（加锁执行）。
- `get_schema_name(user_id, datasource_id, datasource_name="", db_name="") -> str`：schema 隔离命名，优先级 `db_name > datasource_name > user_hash`，格式 `<安全名>_<数据源ID前8位>`。
- `close()`：关闭连接。

> ⚠️ 注意：类上**没有 `execute_query` 方法**（多 Agent 的 SQLAgent 引用该方法会失败，应使用 `fetchall`）。

## `security.py` — 认证安全

| 函数 | 说明 |
|------|------|
| `hash_password(pw) -> str` | bcrypt，截断 72 字节 |
| `verify_password(plain, hashed) -> bool` | 校验 |
| `create_access_token(subject, extra_claims) -> str` | JWT，type=access，120 分钟 |
| `create_refresh_token(subject) -> str` | JWT，type=refresh，7 天 |
| `decode_token(token) -> dict \| None` | 解码失败返回 None |

## `middleware.py` — 中间件 + 日志

- `RequestTimingMiddleware`：perf_counter 统计整请求耗时，structlog JSON 输出（method/path/status/duration_ms）。
- `OperationLogMiddleware`：拦截 `/api/` 请求（跳过 health/docs 等），`BackgroundTask(_write_log)` 后台写 `operation_logs`（失败仅 warning，不阻塞响应）。
- `structlog.configure(...)`：ISO 时间戳 + JSON 渲染。

## `operation_log.py` — 审计标签推断

- `parse_action(method, path) -> (resource_type, action)`：`/api/v1` 前缀剥离后，首段映射资源类型（auth/user/datasource/canvas/dashboard/...），action 形如 `auth.login` / `canvas.create` / `ai.query`；GET 无子路径归为 `{resource}.list`。
- `should_skip(path)`：跳过 /health /docs /openapi /redoc /favicon.ico。

## `sse.py` — 用户级 SSE 连接池

```python
class SSEManager:
    async def subscribe(user_id) -> asyncio.Queue     # 队列上限 100
    async def unsubscribe(user_id, q)
    async def publish(user_id, event, data) -> int    # 广播，队列满丢最旧
    def format_event(event, data) -> str              # "event: x\ndata: json\n\n"

sse_manager = SSEManager()  # 单例
```

## `limiter.py` — 限流

```python
limiter = Limiter(key_func=get_remote_address)  # slowapi，按客户端 IP
# 规则在路由装饰器：/auth/login 5/分钟；/datasources/upload 10/分钟；/ai/sessions/{id}/messages 30/分钟
```

## `config.py` — 配置中心（归属 Core 侧，见 04）

关键项：

| 配置 | 默认 | 说明 |
|------|------|------|
| `DATABASE_URL` | 必填 | asyncpg URL |
| `DUCKDB_DATA_DIR` | `./data/duckdb` | DuckDB 文件目录 |
| `DUCKDB_MEMORY_LIMIT` | 2GB | |
| `openai_api_key` / `openai_model` | / `deepseek-v4-flash` | `is_ai_configured` 派生 |
| `openai_base_url` | `https://api.openai.com/v1` | 可切 DeepSeek 等 |
| `LLM_MODEL_SIMPLE` / `LLM_MODEL_COMPLEX` | 跟随 openai_model | `model_for_task()` 分级路由 |
| `AGENT_ORCHESTRATOR_ENABLED` | True | 多 Agent Feature Flag |
| `JWT_SECRET_KEY` / `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | / 120 / 7 | |
| `redis_url` / `redis_ttl` | `redis://localhost:6379/0` / 300 | |
| `minio_endpoint` / bucket | localhost:9000 / lvco-uploads、lvco-reports | |
| `MAX_UPLOAD_SIZE_MB` / `UPLOAD_DIR` | 100 / ./data/uploads | |
| `LANGFUSE_ENABLED` / key / host | False / / cloud.langfuse.com | 三者齐备才初始化 |
| `CORS_ORIGINS` | 逗号分隔 | 默认含 3000/5173/5175 |

`model_for_task(task_type)`：`simple/polish/clean/recommend` → `LLM_MODEL_SIMPLE`；`agent_stream/insights/sql/planner/chart` 等 → `LLM_MODEL_COMPLEX`；未配置回退 `openai_model`。

## 可观测性（`services/observability.py`，与 core 同属横切）

- `Observer.trace(name, user_id, session_id, metadata)`：上下文管理器产出 `TraceRecord`。
- `observe_llm_call(trace, name, *, messages, model, temperature)`：LLM generation span。
- `observe_tool_call(trace, tool_name, *, args)`：工具调用 span。
- Langfuse 按需初始化（`is_langfuse_configured`），失败/未配置自动降级为本地日志统计（SpanRecord/TraceRecord 保留 latency_ms）；`get_observer()` 单例，`flush()` 请求结束冲刷。
