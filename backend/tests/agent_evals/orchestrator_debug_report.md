# Agent 评测 Baseline 报告

**生成时间**：2026-08-18 22:55:36
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：1328.8s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 8 / 25 | **32.0%** |
| 工具调用成功率 | 21 / 25 | **84.0%** |
| 输出有效率 | 16 / 25 | **64.0%** |
| 图表类型正确率 | 8 / 25 | **32.0%** |
| 图表配置合法率 | 9 / 25 | **36.0%** |
| 平均迭代轮数 | 3.08 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 1 | 2 | 1 |
| anomaly | 2 | 1 | 2 | 2 |
| comparison | 4 | 0 | 2 | 2 |
| distribution | 5 | 2 | 4 | 5 |
| kpi | 4 | 2 | 4 | 1 |
| ranking | 3 | 1 | 2 | 3 |
| trend | 5 | 1 | 5 | 2 |

## 失败案例（top 10）

### Q001 - 今年总销售额是多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`` (期望 `kpi_card`)
- 备注：`result_set_mismatch: expected_rows=1 actual_rows=1; response_len=0 keywords_missing=['总销售额']`

### Q005 - 对比华东和华北区本月的销售额

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`` (期望 `grouped_bar`)
- 备注：`result_set_mismatch: expected_rows=2 actual_rows=1; response_len=0 keywords_missing=['华东', '华北', '对比']`

### Q006 - 每个月的平均订单金额

- 类别：`aggregation`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`` (期望 `line`)
- 备注：`result_set_mismatch: expected_rows=13 actual_rows=1; response_len=0 keywords_missing=['月', '平均']`

### Q008 - 最近 7 天每天的订单数

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`` (期望 `line`)
- 备注：`sql_keyword_overlap<0.6; response_len=0 keywords_missing=['订单数', '7天', '每天']`

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`` (期望 `kpi_card`)
- 备注：`response_len=0 keywords_missing=['独立客户']`

### Q010 - 客单价最高的 5 个产品类别

- 类别：`ranking`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `bar`)
- 备注：`tool_calls=[] query_ok=False`

### Q011 - 各销售渠道的销售额对比

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `grouped_bar`)
- 备注：`tool_calls=['list_datasources'] query_ok=False`

### Q012 - 订单金额分布（按金额段分组）

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `bar`)
- 备注：`tool_calls=['list_datasources'] query_ok=False`

### Q013 - 本季度销售额与上季度对比

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`` (期望 `grouped_bar`)
- 备注：`result_set_mismatch: expected_rows=3 actual_rows=1; response_len=0 keywords_missing=['季度', '对比', '环比']`

### Q015 - 销售额最低的 3 个地区

- 类别：`anomaly`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：✅
- 图表类型：`` (期望 `bar`)
