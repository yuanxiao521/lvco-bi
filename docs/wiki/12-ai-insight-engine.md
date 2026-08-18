# 12 · 智能洞察引擎

> 目录：`services/insight_engine/`（6 个模块）+ `api/v1/insights.py`（路由）。
> 全流程：**自动发现 → 建议 → 建规则 → 定时/手动执行 → 统计异常检测 → LLM 解读 → 洞察报告 → 通知推送**。

## 模块职责

| 文件 | 职责 |
|------|------|
| `auto_discovery.py` | 扫描 PostgreSQL 数据源 schema（经 DuckDB ATTACH），启发式打分识别可监控表，生成 `InsightSuggestion` |
| `detector.py` | 纯统计异常检测器：z-score / 环比(WoW) / 同比(YoY) / 移动平均偏离四类检测 |
| `interpreter.py` | LLM 异常解读：组装 prompt → LLM 生成 Markdown 叙述 + 摘要 + highlights；LLM 失败/解析失败降级规则拼接 |
| `report_generator.py` | 把成功的 InsightRecord 转成 `Report`（source_type=ai_insight），snapshot_blocks = AI 叙述 + 趋势图 + 异常表 + 原始数据表 |
| `runner.py` | 规则执行器：一次 run() 完成「查数据→检测→解读→持久化→更新规则→生成 Report→推送通知」，失败不抛异常 |
| `scheduler.py` | APScheduler 封装：CronTrigger 构建、UUID→PG advisory lock、规则重载/移除/关闭（未启动 noop） |

## 检测算法（`detector.py`，纯标准库实现）

核心数据结构：`TimePoint(timestamp, values: dict[field→float])`、`Anomaly(type, field, severity, current_value, expected_value, deviation, direction, description)`。

### 阈值参数

| 参数 | 默认 | 含义 |
|------|------|------|
| `z_score` | 2.5 | z-score 绝对值阈值 |
| `wow_change` | 0.20 | 环比变化率 |
| `yoy_change` | 0.30 | 同比变化率 |
| `ma_window` | 7 | 移动平均窗口（天） |
| `ma_deviation` | 0.15 | 移动平均偏离 |
| `min_history` | 7 | 最少历史点数 |

### 四类检测器（`detect_anomalies()` 按序执行，数据不足自动跳过）

| 检测器 | 触发条件 | 基准 |
|--------|---------|------|
| `detect_z_score` | ≥8 点，`\|z\|>=2.5`，z=(current-mean)/stdev（样本标准差，历史值算） | expected=mean |
| `detect_wow`（环比） | ≥8 天，`\|(cur - s[-8])\|/s[-8] >= 20%` | 7 天前 |
| `detect_yoy`（同比） | ≥365 点，变化率 ≥30% | 去年同期 |
| `detect_moving_average` | ≥8 点，最新值偏离最近 7 天 MA ≥15% | MA（不含最新点） |

严重性分级：`|deviation|/threshold >= 2.0` → critical；`>=1.0` → warning；否则 info；direction 为 up/down。

## 自动发现（`auto_discovery.py`，仅 PG）

1. ATTACH → 单查询拉全部 public 表列 → 逐表 COUNT 估行数；
2. 剔除 `_id/id/uuid` 等键列（防止 int 型 id 误判为度量）；
3. `discover_candidates` 打分：时间字段 +0.4、度量字段 +0.4、行数≥30 +0.1、≥365 +0.1（满分 1.0）；
4. 生成 `InsightSuggestion`（pending，预置 30 天窗口、前 3 个度量 SUM 聚合的 suggested_config）。

## 调度（`scheduler.py`）

- `_build_trigger`：daily / weekly(周一) / monthly(每月 1 日) → CronTrigger。
- `_advisory_lock_key`：UUID 高 64 位转有符号 bigint，作为 PG advisory lock 分布式锁防重复执行。
- 目前为轻量实现：未启动时 reload/remove/shutdown 均 noop；手动运行走 `POST /insights/rules/{id}/run`（建 pending 记录）。

## 执行流程（`runner.run`）

```
建 running 记录
→ 查数据（BETWEEN period 的 GROUP BY 时间序列）
→ detect_anomalies（四类检测器）
→ LLMInterpreter.interpret（temperature 0.4 / max_tokens 1500，INSIGHT_REPORT_SYSTEM，
   要求严格 JSON 输出 narrative+summary+highlights，强制"引用真实数字、不编造"）
→ 更新记录/规则 last_run_at/next_run_at
→ ReportGenerator 生成 Report（title "{rule.name} - {YYYY-MM-DD}"，snapshot_blocks：
   markdown 叙述块 + line 趋势图 + 异常明细表(8列) + 原始数据表(30行)，status=published）
→ 推送 Notification（insight_ready / insight_failed）
```

- 数据拆分：`_split_current_historical(series, current_days=7)` 把最近 7 天作为"当前周期"给 LLM，历史传完整序列（检测器需要完整序列算均值/MA）。
- LLM 兜底：未配置 / 上游报错 / JSON 解析失败 → `interpreter` 走 `_fallback_narrative` 规则拼接（哨兵 `"(LLM 响应解析失败)"`）。
- 安全：`_build_query_sql` 对表名/字段名正则白名单（`^[A-Za-z_][A-Za-z0-9_]{0,62}$`）+ 双引号防注入；聚合函数白名单 SUM/AVG/MAX/MIN/COUNT/COUNT_DISTINCT。

## API 路由（`api/v1/insights.py`）

见 [05-backend-api.md](./05-backend-api.md)「智能洞察」一节：规则 CRUD、手动运行、`discover/{datasource_id}` 扫描、建议 accept/dismiss（接受自动建规则）。

## 表结构速查

- `insight_rules`：query_config（table/time_field/measures/dimensions/time_range_days）、detect_types（默认 ["anomaly","trend","ratio"]）、schedule、next_run_at。
- `insight_records`：ai_narrative、charts、raw_data（series 限 50 点 + total_points）、detected_anomalies、llm_model / tokens、report_id。
- `insight_suggestions`：table_name / time_field / measure_fields / suggested_config / confidence / status。
