# Agent 评测 Baseline 报告

**生成时间**：2026-08-15 12:10:41
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：406.9s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 12 / 25 | **48.0%** |
| 工具调用成功率 | 24 / 25 | **96.0%** |
| 输出有效率 | 13 / 25 | **52.0%** |
| 图表类型正确率 | 14 / 25 | **56.0%** |
| 图表配置合法率 | 25 / 25 | **100.0%** |
| 平均迭代轮数 | 3.64 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 0 | 2 | 1 |
| anomaly | 2 | 2 | 2 | 2 |
| comparison | 4 | 1 | 4 | 3 |
| distribution | 5 | 4 | 5 | 3 |
| kpi | 4 | 1 | 4 | 2 |
| ranking | 3 | 3 | 3 | 0 |
| trend | 5 | 1 | 4 | 2 |

## 失败案例（top 10）

### Q001 - 今年总销售额是多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `kpi_card`)
- 备注：`response_len=539 keywords_missing=['SUM']; expected=kpi_card actual=line`

### Q002 - 最近 30 天的销售额趋势

- 类别：`trend`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=754 keywords_missing=['每天', 'SUM']`

### Q003 - 各地区的销售额占比

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`donut` (期望 `pie`)
- 备注：`response_len=708 keywords_missing=['SUM']; expected=pie actual=donut`

### Q004 - 销售额前 10 的产品

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`horizontal_bar` (期望 `bar`)
- 备注：`response_len=909 keywords_missing=['前10', 'SUM']; expected=bar actual=horizontal_bar`

### Q005 - 对比华东和华北区本月的销售额

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`grouped_bar` (期望 `grouped_bar`)

### Q006 - 每个月的平均订单金额

- 类别：`aggregation`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=838 keywords_missing=['AVG']`

### Q007 - 客户年龄段分布

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`grouped_bar` (期望 `bar`)
- 备注：`response_len=974 keywords_missing=['COUNT']; expected=bar actual=grouped_bar`

### Q008 - 最近 7 天每天的订单数

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=453 keywords_missing=['7天', '每天']`

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`kpi_card` (期望 `kpi_card`)
- 备注：`response_len=243 keywords_missing=['COUNT_DISTINCT']`

### Q010 - 客单价最高的 5 个产品类别

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=551 keywords_missing=['AVG']`
