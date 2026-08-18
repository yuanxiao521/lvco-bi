# 15 · 运行与部署

## 环境要求

- Python 3.10–3.13（开发 3.12）、Node.js 22+、Docker（PostgreSQL/Redis/MinIO）或本地安装
- LLM API Key（OpenAI 兼容，如 DeepSeek）；可选 Langfuse 公钥/密钥

## 一、本地开发

### 1. 数据库

```powershell
cd e:\BI\LvcoBI\lvco-bi
docker compose up -d postgres redis        # 可选: 再加 minio
# postgres:16-alpine, 库 lvco_bi, 账号 lvco/lvco_secret, 端口 5432; redis 6379
```

### 2. 后端环境变量

```powershell
cd backend
Copy-Item .env.example .env
# 编辑 .env：
#   DATABASE_URL=postgresql+asyncpg://lvco:lvco_secret@localhost:5432/lvco_bi
#   JWT_SECRET_KEY=<64位随机串>
#   OPENAI_API_KEY=sk-xxx
#   OPENAI_BASE_URL=https://api.deepseek.com
#   OPENAI_MODEL=deepseek-v4-flash
# （可参考 .env.backup 中实际可用的 DeepSeek 配置）
```

### 3. 依赖与迁移

```powershell
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt            # 或 uv sync（uv.lock）
alembic upgrade head                       # 执行全部迁移（13 个版本）
```

### 4. 启动后端

```powershell
powershell .\start_backend.ps1             # uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或: uvicorn app.main:app --reload --port 8000
# 健康检查 http://localhost:8000/health ; Swagger http://localhost:8000/docs
```

### 5. 生成并上传模拟数据（可选）

```powershell
python scripts/generate_mock_data.py       # 生成 6 个 CSV 到 mock_data/
python scripts/upload_mock_data.py         # 注册/登录 test@example.com / test123456，批量上传
```

### 6. 启动前端

```powershell
cd ..\frontend
npm install
# 注意：client.ts 默认 baseURL 指向 8001，本地需建 .env：
#   VITE_API_BASE_URL=http://localhost:8000/api/v1
npm run dev                                # Vite http://localhost:5173 (strictPort)
```

浏览器访问 `http://localhost:5173`，注册账号即可使用。

> ⚠️ **端口坑**：后端 8000（start_backend.ps1） vs 前端默认 8001，必须用 `VITE_API_BASE_URL` 对齐。

## 二、Docker 部署（生产）

`docker-compose.yml`（dev 基准）5 个服务：nginx(80) + frontend(node:22-alpine 跑 Vite dev server:3000) + backend(python:3.12-slim, uvicorn:8000) + postgres(16-alpine, healthcheck) + redis(7) + minio(9000/9001)。

`docker-compose.prod.yml`（生产叠加）：清空源码挂载（`volumes: []`）、backend 显式 command、全部 `restart: always` + healthcheck。

```powershell
Copy-Item .env.prod.example .env           # 填写 DATABASE_URL 指向 postgres 服务名、OPENAI_*、MINIO_ENDPOINT=minio:9000、REDIS_URL=redis://redis:6379/0
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose exec backend alembic upgrade head      # 首次执行迁移
docker compose exec backend python scripts/generate_mock_data.py
docker compose exec backend python scripts/upload_mock_data.py --base-url http://localhost/api/v1
```

### nginx 关键配置（`nginx.conf`）

- `/` → frontend:3000；`/api/` → backend:8000；`/docs` 透传。
- **`/api/v1/ai/` 单独 location：`proxy_buffering off` + `proxy_read_timeout 300s`**（保证 SSE 不缓冲）。

## 三、测试

```powershell
cd backend
pytest tests/ -v                            # 服务/仓储/洞察引擎单测
python tests/run_all_tests.py               # 全量自动测试
python tests/agent_evals/run_eval.py        # Agent 评估（dataset.jsonl + judge.py）
```

测试覆盖示例：`test_prompt_registry.py`（Prompt 注册中心）、`test_observability.py`、`test_repositories.py`、`test_dashboard_service.py`、`test_statistics_refactor.py`、`insight_engine/*` 六个模块单测。

## 四、常见排障

| 现象 | 处理 |
|------|------|
| AI 接口 503 `AI_NOT_CONFIGURED` | .env 未配 OPENAI_API_KEY |
| 前端 401 循环 | 检查 refresh token 逻辑与 CORS_ORIGINS |
| SSE 卡顿/缓冲 | nginx 需命中 `/api/v1/ai/` location 的 proxy_buffering off |
| 多 Agent 查询失败 | `duckdb_client.execute_query` 不存在，改用 `fetchall`（已知缺陷） |
| PG 枚举迁移失败 | 0003/0008/0010 用 autocommit_block，确保 PG ≥ 11 |
