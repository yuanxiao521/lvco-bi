# Agent 评测 Baseline 报告

**生成时间**：2026-08-14 18:46:39
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：466.4s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 0 / 25 | **0.0%** |
| 工具调用成功率 | 24 / 25 | **96.0%** |
| 输出有效率 | 11 / 25 | **44.0%** |
| 图表类型正确率 | 9 / 25 | **36.0%** |
| 图表配置合法率 | 25 / 25 | **100.0%** |
| 平均迭代轮数 | 3.92 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 0 | 2 | 1 |
| anomaly | 2 | 0 | 2 | 2 |
| comparison | 4 | 0 | 4 | 3 |
| distribution | 5 | 0 | 5 | 3 |
| kpi | 4 | 0 | 4 | 1 |
| ranking | 3 | 0 | 3 | 0 |
| trend | 5 | 0 | 4 | 1 |

## 失败案例（top 10）

### Q001 - 今年总销售额是多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `kpi_card`)
- 备注：`response_len=364 keywords_missing=['SUM']; expected=kpi_card actual=bar`

### Q002 - 最近 30 天的销售额趋势

- 类别：`trend`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：❌
- 图表类型：`bar` (期望 `line`)
- 备注：`tool_calls=['list_datasources', 'query_datasource', 'render_chart', 'render_chart', 'recommend_charts', 'render_chart'] has_error=True; response_len=808 keywords_missing=['每天', 'SUM']; expected=line actual=bar`

### Q003 - 各地区的销售额占比

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`donut` (期望 `pie`)
- 备注：`response_len=744 keywords_missing=['地区', 'SUM']; expected=pie actual=donut`

### Q004 - 销售额前 10 的产品

- 类别：`ranking`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=847 keywords_missing=['前10', 'SUM']`

### Q005 - 对比华东和华北区本月的销售额

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `grouped_bar`)
- 备注：`expected=grouped_bar actual=bar`

### Q006 - 每个月的平均订单金额

- 类别：`aggregation`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `line`)
- 备注：`response_len=902 keywords_missing=['AVG']; expected=line actual=bar`

### Q007 - 客户年龄段分布

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=870 keywords_missing=['COUNT']`

### Q008 - 最近 7 天每天的订单数

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `line`)
- 备注：`response_len=514 keywords_missing=['7天', '每天']; expected=line actual=bar`

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `kpi_card`)
- 备注：`response_len=380 keywords_missing=['COUNT_DISTINCT']; expected=kpi_card actual=bar`

### Q010 - 客单价最高的 5 个产品类别

- 类别：`ranking`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=630 keywords_missing=['AVG']`
