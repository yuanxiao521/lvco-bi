# 05 · REST API 路由全清单

> 所有路由前缀 `/api/v1`（`router.py` 聚合），除注明外均需 Bearer Token。
> 完整签名以 Swagger（`/docs`）为准，此处为功能级清单。

## 认证 `auth.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/auth/login` | 登录，返回 access/refresh token + 用户信息（限流 5/分钟） |
| POST | `/auth/register` | 注册（201），邮箱冲突 409 |
| POST | `/auth/refresh` | refresh token 换新 access token |
| POST | `/auth/logout` | 登出 |
| POST | `/auth/change-password` | 改密码（旧密码校验，新密码 ≥8 位） |
| PATCH | `/auth/profile` | 更新 display_name / email |

## 数据源 `datasources.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/datasources` | 分页列表（type/status/search 过滤） |
| GET | `/datasources/{id}` | 详情 |
| POST | `/datasources/upload` | 上传 CSV/Excel（限流 10/分钟） |
| POST | `/datasources/connect` | 连接 MySQL/PostgreSQL（密码 AES 加密存储） |
| POST | `/datasources/{id}/disconnect` | 置为断开 |
| POST | `/datasources/{id}/sync` | 同步（拉 schema） |
| PATCH | `/datasources/{id}/schema` | 更新 schema_meta |
| DELETE | `/datasources/{id}` | 删除 |
| GET | `/datasources/{id}/preview` | 预览前 N 行 |
| GET | `/datasources/{id}/tables` | 列出 PG public 表 |
| POST | `/datasources/{id}/ai-clean` | AI 数据质量分析（LLM 失败走规则兜底） |
| POST | `/datasources/test-connection` | 不落库测试连接 |

## 画布 `canvases.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| GET/POST | `/canvases` | 分页列表 / 创建 |
| GET/PATCH/DELETE | `/canvases/{id}` | 详情 / 改标题 / 删除（软删） |
| PUT | `/canvases/{id}/blocks` | 更新画布 blocks |
| POST | `/canvases/{id}/query` | 执行图表查询（多数据源） |
| POST | `/canvases/{id}/chart-configs` | 创建图表配置 |
| POST | `/canvases/{id}/save-as-report` | 画布存为报表快照 |
| POST | `/canvases/{id}/pin-to-dashboard` | 钉到仪表盘 |
| GET | `/canvases/{id}/export/pdf` | 导出 PDF（子进程 Playwright） |
| POST | `/canvases/ai-recommend` | 按数据源推荐图表（无画布） |
| POST | `/canvases/{id}/ai-recommend` | 画布内 AI 推荐图表 |
| POST | `/canvases/{id}/ai-image` | AI 配图（占位） |

## 仪表盘 `dashboards.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| GET/POST | `/dashboards` | 列表（search 过滤，带图表数）/ 创建 |
| GET/PUT/DELETE | `/dashboards/{id}` | 详情（含图表）/ 更新布局 / 删除（软删） |
| POST | `/dashboards/{id}/charts` | 添加图表 |
| DELETE | `/dashboards/{id}/charts/{chart_id}` | 移除图表 |
| GET | `/dashboards/{id}/data` | 获取聚合数据（缓存） |
| POST | `/dashboards/{id}/refresh` | 强制刷新数据（use_cache=False） |
| POST | `/dashboards/{id}/share` | 生成 30 天 share_token |

## 报表 `reports.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| GET/POST | `/reports` | 列表（source_type/status 过滤）/ 创建 |
| GET/PATCH | `/reports/{id}` | 详情（含 snapshotBlocks）/ 改标题 |
| PATCH | `/reports/{id}/status` | 改状态 |
| POST | `/reports/{id}/share` | 生成分享 token |
| GET | `/reports/{id}/export/pdf` | PDF：MinIO 预签名 URL → 直接文件 → HTML 回退 |
| DELETE | `/reports/{id}` | 删除（deleted 状态） |

## AI `ai.py`（核心）

| 方法 | 路径 | 用途 |
|------|------|------|
| GET/POST | `/ai/sessions` | 会话列表 / 创建 |
| PATCH/GET/DELETE | `/ai/sessions/{id}` | 重命名 / 详情 / 删除 |
| GET | `/ai/sessions/{id}/messages` | 消息列表 |
| POST | `/ai/sessions/{id}/messages` | 普通对话流式（SSE，限流 30/分钟，自动摘要 chart 代码块） |
| POST | `/ai/chat/stream` | **Agent 数据对话（SSE）**：AgentOrchestrator / 降级 AIService，状态文字过滤、图表去重 |
| POST | `/ai/clean` | 清洗预览（返回将影响的行） |
| POST | `/ai/clean/apply` | 执行清洗（仅 CSV/Excel；drop_null/drop_negative/fill_mean/fill_median/fill_mode/fill_ffill/standardize_date） |
| POST | `/ai/query` | 自然语言→SQL→DuckDB（仅 SELECT，最多 50 行） |
| POST | `/ai/insights` | 基于查询结果生成洞察（LLM 失败返回空） |
| POST | `/ai/polish` | 文本润色（professional/casual/concise/academic） |
| POST | `/ai/canvas/chat` | 画布场景 AI 对话（SSE，解析 sql/json，字段与图表类型校验） |

**`/ai/chat/stream` SSE 事件类型**：`session_created` / `status` / `plan` / `message`（文本增量）/ `sql_result` / `chart` / `text`（报告）/ `query_error` / `warning` / `done`（携带全部 charts）/ `error`。

## 统计 `statistics.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| POST | `/statistics/describe` | 描述统计（count/mean/std/min/max/p25/p50/p75/null_rate） |
| POST | `/statistics/correlation` | Pearson 相关矩阵（NaN/Inf 归零） |
| POST | `/statistics/ranking` | Top N / Bottom N 排名 |
| POST | `/statistics/summary` | 汇总（行数/列数/去重键/日期范围） |
| POST | `/statistics/comparison` | 同比/环比（DuckDB LAG 窗口函数） |
| POST | `/statistics/preview` | 快速预览（≤20 行） |

## 智能洞察 `insights.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| GET/POST | `/insights/rules` | 规则列表（enabled 过滤）/ 创建（计算 next_run_at） |
| GET/PATCH/DELETE | `/insights/rules/{rule_id}` | 详情 / 更新 / 删除 |
| POST | `/insights/rules/{rule_id}/run` | 手动运行（建 pending 记录） |
| POST | `/insights/discover/{datasource_id}` | 扫描 PG 数据源生成建议（仅 PG） |
| GET | `/insights/suggestions` | 建议列表（status/datasource 过滤） |
| POST | `/insights/suggestions/{id}/accept` | 接受建议 → 自动建规则 |
| POST | `/insights/suggestions/{id}/dismiss` | 忽略建议 |

## 通知 `notifications.py`

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/notifications` | 列表（unread_only 过滤 + unreadCount） |
| GET | `/notifications/unread_count` | 未读数 |
| POST | `/notifications/{id}/read` | 标记已读 |
| POST | `/notifications/read_all` | 全部已读 |
| DELETE | `/notifications` | 清空 |
| POST | `/notifications/push` | 前端推送通知 |
| GET | `/notifications/stream` | **SSE 实时流**（?token= 查询参数，30s 心跳） |

## 权限 / 审计 / 回收站 / 公开

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/permissions/users` | 用户列表（search/role 过滤 + 资源计数） |
| PATCH | `/permissions/users/{user_id}/role` | 改角色（仅 admin，LAST_ADMIN 防呆） |
| GET | `/audit/logs` | 审计日志查询（多条件） |
| GET | `/audit/logs/summary` | 最近 24h 汇总 |
| GET | `/audit/logs/export` | CSV 导出（UTF-8 BOM，上限 1 万条） |
| GET | `/trash` | 回收站列表 |
| POST | `/trash/{type}/{id}/restore` | 恢复 |
| DELETE | `/trash/{type}/{id}` | 彻底删除（仅 admin） |
| GET | `/public/share/{token}` | 免认证查看分享的仪表盘/报表 |
| GET | `/health` | 健康检查（无鉴权） |

## 中间件管线

```
OperationLogMiddleware → RequestTimingMiddleware → CORSMiddleware → 路由
（Starlette 后注册者包外层，故 OperationLog 最外层、最先处理请求）
```

- **OperationLogMiddleware**：拦截 `/api/` 请求，后台 `BackgroundTask` 写 `operation_logs`（失败仅 warning，不阻塞）。
- **RequestTimingMiddleware**：`duration_ms` 输出 structlog JSON 日志。
- **异常处理器**：限流 → 429 `{code:"RATE_LIMITED"}` + Retry-After；兜底 → 500 `{code:"INTERNAL_ERROR"}`。
