# Agent 评测 Baseline 报告

**生成时间**：2026-08-18 20:03:34
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：746.3s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 4 / 25 | **16.0%** |
| 工具调用成功率 | 3 / 25 | **12.0%** |
| 输出有效率 | 22 / 25 | **88.0%** |
| 图表类型正确率 | 4 / 25 | **16.0%** |
| 图表配置合法率 | 4 / 25 | **16.0%** |
| 平均迭代轮数 | 2.08 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 0 | 0 | 2 |
| anomaly | 2 | 0 | 0 | 2 |
| comparison | 4 | 0 | 0 | 3 |
| distribution | 5 | 0 | 1 | 5 |
| kpi | 4 | 2 | 1 | 3 |
| ranking | 3 | 1 | 0 | 2 |
| trend | 5 | 1 | 1 | 5 |

## 失败案例（top 10）

### Q001 - 今年总销售额是多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `kpi_card`)
- 备注：`tool_calls=['list_datasources', 'list_datasources'] has_error=False`

### Q002 - 最近 30 天的销售额趋势

- 类别：`trend`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `line`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q003 - 各地区的销售额占比

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `pie`)
- 备注：`tool_calls=['list_datasources', 'query_engine', 'list_datasources'] has_error=True`

### Q004 - 销售额前 10 的产品

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：❌
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`tool_calls=['list_datasources', 'query_datasource', 'query_datasource', 'render_chart'] has_error=True; response_len=0 keywords_missing=['前10', '产品']`

### Q005 - 对比华东和华北区本月的销售额

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `grouped_bar`)
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
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `bar`)

### Q008 - 最近 7 天每天的订单数

- 类别：`trend`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `line`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：✅
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `kpi_card`)
- 备注：`tool_calls=['list_datasources', 'query_datasource', 'query_datasource'] has_error=True`

### Q010 - 客单价最高的 5 个产品类别

- 类别：`ranking`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `bar`)
- 备注：`tool_calls=['list_datasources'] has_error=False`
