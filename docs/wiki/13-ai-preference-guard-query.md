# 13 · 用户偏好记忆、SQL 安全防护与查询引擎

## 一、用户偏好记忆（`services/user_preference_service.py`）

### 偏好类型与衰减

```python
CHART_TYPE / COLOR_SCHEME / DIMENSION / AGGREGATION / ANALYSIS_FOCUS   # 5 类偏好
DECAY_DAYS = 30      # 30 天未使用开始衰减
DECAY_RATE = 0.95    # 每 30 天 strength × 0.95
MIN_STRENGTH = 0.1   # 低于此值删除
```

### 存储（表 `user_preferences`）

`user_id` / `preference_type` / `preference_key`（如 line/bar/blue）/ `preference_value`(JSON) / `strength`(0-1) / `evidence_count` / `last_used_at`。

### 核心方法

| 方法 | 行为 |
|------|------|
| `set_preference(user_id, type, key, value, explicit, source)` | upsert：已存在则 evidence_count+1；显式 strength=1.0；隐式 min(1.0, strength+0.1)；刷新 last_used_at |
| `record_explicit_preference(...)` | 显式偏好，strength=1.0 |
| `record_implicit_preference(...)` | 隐式偏好，初始 0.5 |
| `apply_decay(user_id)` | 每 30 天衰减一次，低于 0.1 删除 |
| `get_top_preferences(user_id, type, limit=3)` | 按 strength 排序取 Top N |
| `format_preferences_for_prompt(...)` | 输出「- 图表类型: line(85%), bar(60%)」文本 |

### 注入机制（读取侧已生效）

`AIService.recommend_charts` 拿到 user_id 后加载全部偏好，取前 5 条构造 `## 用户偏好` hint 拼进 RECOMMEND_SYSTEM 的 user 消息：chart_type → "用户偏好使用 {value} 类型图表"、color_scheme → "用户偏好 {value} 配色方案"、dimension → "用户偏好以 {value} 作为维度"。读取失败仅告警不影响主流程。

### 现状说明

- **写入侧**（record_implicit/explicit_preference）逻辑完备但 **API 层尚未接线**（无端点调用）；衰减方法亦未接入定时任务。
- 多 Agent 的 chart_agent 预留 `chart_preferences` 参数，但编排器调用时未传入（当前为 `{}`）。

## 二、SQL 三层安全防护（`services/sql_guard.py`）

```python
class GuardResult(allowed, layer=0, reason="", sanitized_input="", sanitized_sql="")
sql_guard = SQLGuard()   # 模块级单例
```

| 层 | 方法 | 机制 |
|----|------|------|
| L1 输入净化 | `sanitize_input(user_input)` | 空输入 / 2000 字截断 / 6 条 prompt 注入正则（"忽略…指令"、"你是…开发者"、"输出 system prompt"、DAN/jailbreak、`[INST]` 等）/ 4 条危险 SQL 模式（`;DROP`、`UNION SELECT`、`information_schema`、`xp_cmdshell`）/ 控制字符剔除 |
| L2 意图分析 | `analyze_intent(user_input)` | 三组关键词：写操作（删除/修改/drop 等，英文需 table/database/from 语境）、提权（grant/revoke/create user）、脱库（全部数据/dump/pg_shadow） |
| L3 SQL 输出控制 | `validate_sql(sql)` | 必须 SELECT 开头 → 16 个黑名单关键字（DROP/DELETE/INSERT/UPDATE/ALTER/TRUNCATE/CREATE/GRANT/REVOKE/EXEC/ATTACH/DETACH/PRAGMA/INSTALL/LOAD/CALL/COPY/VACUUM/CHECKPOINT）→ 剥离字符串字面量后查分号（防多语句）→ **自动补 LIMIT 100** |

- `full_check(user_input, generated_sql="")`：全链路 L1→L2→L3；输入为空（工具内部调用场景）自动跳过 L1/L2。
- **应用范围差异**：
  - 单 Agent 的 `query_datasource` 工具 → `full_check("", sql)` 含 L3（安全）；
  - 多 Agent 的 SQLAgent → 只对用户输入做 L1+L2，生成的 SQL 未过 L3（缺陷点）；
  - `/ai/query` 路由 → SELECT 白名单 + 最多 50 行。

## 三、统一查询引擎（`services/query_engine.py`）

### 核心函数

```python
async def execute_chart_query(datasource_id, config: ChartQueryConfig, user_id, db=None) -> QueryResult
# → QueryResult(columns, rows, chart_type, query_time_ms)
```

### 执行流程

1. 查数据源 → PG/MySQL 解密并 ATTACH → `_ensure_datasource_ready`（PG 三部分名绕过 scanner 限制）；
2. 自动同步 schemaMeta（DuckDB 重启后列漂移修正）；
3. MD5 缓存（key `query:{ds}:{user}:{hash}`）；
4. 字段/聚合/操作符校验 → `_build_select/_build_where/_build_group_by/_build_order_by` 动态拼 SQL（**全部参数化 `?` 占位**）→ `asyncio.to_thread(duckdb_client.fetchall, sql, params)`；
5. 缺 LIMIT 自动补 500。

### 白名单常量

- `ALLOWED_AGGREGATIONS`：SUM / AVG / COUNT / MAX / MIN / STDDEV / MEDIAN / COUNT_DISTINCT
- `ALLOWED_OPERATORS`：eq / neq / gt / gte / lt / lte / between / in / like

### 说明

- 本文件无显式查询超时（DuckDB 同步 + 线程锁模型）；超时仅存在于 LLM 客户端（30s）与通知 SSE（30s）。
- 统计接口（`api/v1/statistics.py`）也复用 DuckDB：描述统计、Pearson 相关矩阵、Top/Bottom 排名、同比环比（LAG 窗口函数）、汇总。

## 四、数据质量（`services/data_quality.py`，清洗建议的数据来源）

| 检测方法 | 机制 |
|----------|------|
| `null_count` | NULL 计数 |
| `outlier_iqr_count` | PERCENTILE_CONT 四分位距 1.5×IQR |
| `outlier_count` | Z-Score 3σ |
| `type_inconsistency_count` | TRY_CAST 检测 |
| `dup_row_count` | `COUNT(*) OVER (PARTITION BY *)` 窗口 |
| `format_issue_count` | 日期正则 |

统一输出 `{field, issue_type, count, percentage, sample_values}`；全部 `asyncio.to_thread` 隔离；`DataQualityService(schema_repo)` 构造（依赖 schema 仓储）。
