# Agent 评测 Baseline 报告

**生成时间**：2026-08-18 15:53:17
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：209.7s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 7 / 25 | **28.0%** |
| 工具调用成功率 | 8 / 25 | **32.0%** |
| 输出有效率 | 22 / 25 | **88.0%** |
| 图表类型正确率 | 7 / 25 | **28.0%** |
| 图表配置合法率 | 10 / 25 | **40.0%** |
| 平均迭代轮数 | 1.96 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 0 | 0 | 2 |
| anomaly | 2 | 0 | 0 | 1 |
| comparison | 4 | 2 | 2 | 4 |
| distribution | 5 | 2 | 2 | 5 |
| kpi | 4 | 0 | 0 | 4 |
| ranking | 3 | 2 | 2 | 3 |
| trend | 5 | 1 | 2 | 3 |

## 失败案例（top 10）

### Q001 - 今年总销售额是多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `kpi_card`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q002 - 最近 30 天的销售额趋势

- 类别：`trend`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：❌
- 图表类型：`` (期望 `line`)
- 备注：`tool_calls=['list_datasources'] has_error=False; response_len=317 keywords_missing=['每天']`

### Q004 - 销售额前 10 的产品

- 类别：`ranking`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `bar`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q006 - 每个月的平均订单金额

- 类别：`aggregation`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `line`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q007 - 客户年龄段分布

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `bar`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `kpi_card`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q011 - 各销售渠道的销售额对比

- 类别：`comparison`
- SQL 准确：✅
- 工具成功：❌
- 输出有效：✅
- 图表类型：`bar` (期望 `grouped_bar`)
- 备注：`tool_calls=['list_datasources', 'query_engine', 'query_datasource', 'render_chart'] has_error=True; expected=grouped_bar actual=bar`

### Q012 - 订单金额分布（按金额段分组）

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `bar`)

### Q013 - 本季度销售额与上季度对比

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`grouped_bar` (期望 `grouped_bar`)

### Q014 - 本月新客户数

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `kpi_card`)
- 备注：`tool_calls=['list_datasources'] has_error=False`
