# Agent 评测 Baseline 报告

**生成时间**：2026-08-17 22:10:30
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：283.4s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 21 / 25 | **84.0%** |
| 工具调用成功率 | 24 / 25 | **96.0%** |
| 输出有效率 | 12 / 25 | **48.0%** |
| 图表类型正确率 | 18 / 25 | **72.0%** |
| 图表配置合法率 | 25 / 25 | **100.0%** |
| 平均迭代轮数 | 3.12 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 2 | 2 | 1 |
| anomaly | 2 | 2 | 2 | 2 |
| comparison | 4 | 3 | 4 | 3 |
| distribution | 5 | 4 | 4 | 2 |
| kpi | 4 | 4 | 4 | 2 |
| ranking | 3 | 3 | 3 | 0 |
| trend | 5 | 3 | 5 | 2 |

## 失败案例（top 10）

### Q001 - 今年总销售额是多少？

- 类别：`kpi`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`kpi_card` (期望 `kpi_card`)
- 备注：`response_len=166 keywords_missing=['SUM']`

### Q002 - 最近 30 天的销售额趋势

- 类别：`trend`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=919 keywords_missing=['每天', 'SUM']`

### Q003 - 各地区的销售额占比

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`pie` (期望 `pie`)
- 备注：`response_len=668 keywords_missing=['SUM']`

### Q004 - 销售额前 10 的产品

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=528 keywords_missing=['前10', 'SUM']`

### Q006 - 每个月的平均订单金额

- 类别：`aggregation`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=666 keywords_missing=['AVG']`

### Q007 - 客户年龄段分布

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：❌
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`tool_calls=['list_datasources', 'query_engine', 'query_datasource', 'render_chart'] has_error=True; response_len=668 keywords_missing=['COUNT']`

### Q008 - 最近 7 天每天的订单数

- 类别：`trend`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=438 keywords_missing=['7天', '每天']`

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`kpi_card` (期望 `kpi_card`)
- 备注：`response_len=201 keywords_missing=['COUNT_DISTINCT']`

### Q010 - 客单价最高的 5 个产品类别

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=437 keywords_missing=['AVG']`

### Q011 - 各销售渠道的销售额对比

- 类别：`comparison`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `grouped_bar`)
- 备注：`response_len=387 keywords_missing=['SUM']; expected=grouped_bar actual=bar`
