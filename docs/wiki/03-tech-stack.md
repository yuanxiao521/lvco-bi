# 03 · 技术栈与依赖

## 后端（Python 3.10–3.13，开发环境 3.12）

### 核心框架
| 依赖 | 版本 | 用途 |
|------|------|------|
| fastapi | 0.115.6 | Web 框架 |
| uvicorn[standard] | 0.34.0 | ASGI 服务器 |
| pydantic / pydantic-settings | 2.10.4 / 2.7.1 | 配置与校验 |
| python-multipart | 0.0.20 | 文件上传 |

### 数据访问与分析
| 依赖 | 版本 | 用途 |
|------|------|------|
| sqlalchemy[asyncio] | 2.0.36 | 异步 ORM |
| asyncpg | 0.30.0 | PostgreSQL 异步驱动 |
| alembic | 1.14.1 | 数据库迁移 |
| duckdb | 1.2.0 | 统一分析查询引擎 |
| openpyxl | 3.1.5 | Excel 读取 |
| redis | >=5.0,<6.0 | 缓存 |
| minio | >=7.2,<8.0 | 对象存储（报表导出） |

### LLM / AI
| 依赖 | 版本 | 用途 |
|------|------|------|
| httpx | 0.28.1 | 调用 OpenAI 兼容 Chat Completions（无 openai SDK，裸协议） |
| langfuse | >=2.0,<3.0 | LLM 链路可观测 |
| pyyaml | >=6.0 | prompts/*.yaml 加载 |

### 安全 / 平台
| 依赖 | 版本 | 用途 |
|------|------|------|
| python-jose[cryptography] | 3.3.0 | JWT |
| passlib[bcrypt] | 1.7.4 | 密码哈希 |
| pycryptodome | >=3.20 | 数据源密码 AES 加密 |
| slowapi | >=0.1,<0.2 | 限流 |
| structlog | >=24.4 | 结构化日志 |
| apscheduler | >=3.10,<4.0 | 洞察定时调度 |

### 开发 / 测试（pyproject.toml dev）
pytest、pytest-asyncio、pytest-mock、httpx[test]、ruff、mypy；scripts 里 upload_mock_data 等用到 `requests`（需自行补装）。

## 前端（React 19 + Vite 8 + TS 6）

| 依赖 | 版本 | 用途 |
|------|------|------|
| react / react-dom | 19.2.8 | UI 框架 |
| react-router-dom | ^7.18.1 | 路由 |
| zustand | ^5.0.14 | 状态管理 |
| axios | ^1.18.1 | HTTP（401 自动刷新 token） |
| echarts / echarts-for-react | ^5.5.0 / ^3.0.2 | 图表 |
| recharts | ^3.10.0 | 图表（ChartRenderer 备选渲染器） |
| lucide-react | ^1.25.0 | 图标 |
| tailwindcss | ^4.3.3 | 样式（@tailwindcss/vite 插件） |
| typescript | ~6.0.2 | 语言 |
| vite | ^8.1.1 | 构建（port 5173 strictPort） |
| vitest + @testing-library/react + jsdom | 4.1.10 | 测试 |

## 中间件 / 基础设施

| 组件 | 说明 |
|------|------|
| PostgreSQL 16 | 业务数据库（`lvco_bi`，账号 `lvco`/`lvco_secret`，5432） |
| DuckDB | 本地文件 `{DUCKDB_DATA_DIR}/lvco_bi.duckdb`（默认 `./data/duckdb`），memory_limit 2GB |
| Redis 7 | 缓存 + 通知（6379） |
| MinIO | 报表 PDF（bucket `lvco-reports` / `lvco-uploads`，9000/9001） |
| nginx | 统一入口（80），`/api/v1/ai/` 关闭代理缓冲支持 SSE |

## 关键版本注意点

1. **前后端端口**：后端默认 8000（`start_backend.ps1`），前端 axios `client.ts` 默认 baseURL 指向 **8001**，本地联调需设置 `VITE_API_BASE_URL=http://localhost:8000/api/v1` 或让后端跑 8001。
2. **LLM Provider 可切换**：通过 `OPENAI_BASE_URL`（默认 `https://api.openai.com/v1`，`.env.backup` 里实际用 `https://api.deepseek.com` + `deepseek-v4-flash`）。
3. **模型路由**：`LLM_MODEL_SIMPLE`（简单任务）与 `LLM_MODEL_COMPLEX`（复杂任务），未配置回退 `openai_model`。
4. **requirements.txt 与 pyproject.toml 有差异**：前者为锁定运行快照，后者更完整（含 cryptography、pyjwt、python-dotenv、pandas）。
