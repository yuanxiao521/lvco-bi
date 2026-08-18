# Agent 评测 Baseline 报告

**生成时间**：2026-08-18 20:11:47
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：287.4s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 20 / 25 | **80.0%** |
| 工具调用成功率 | 24 / 25 | **96.0%** |
| 输出有效率 | 25 / 25 | **100.0%** |
| 图表类型正确率 | 16 / 25 | **64.0%** |
| 图表配置合法率 | 25 / 25 | **100.0%** |
| 平均迭代轮数 | 3.16 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 2 | 2 | 2 |
| anomaly | 2 | 1 | 2 | 2 |
| comparison | 4 | 3 | 4 | 4 |
| distribution | 5 | 4 | 5 | 5 |
| kpi | 4 | 4 | 4 | 4 |
| ranking | 3 | 3 | 2 | 3 |
| trend | 5 | 3 | 5 | 5 |

## 失败案例（top 10）

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
- 图表类型：`bar` (期望 `grouped_bar`)
- 备注：`expected=grouped_bar actual=bar`

### Q019 - 华东区各产品的销售额对比

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`horizontal_bar` (期望 `horizontal_bar`)

### Q022 - 每天的退款金额

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`line` (期望 `line`)

### Q023 - 销售额最高的 5 个客户

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：❌
- 输出有效：✅
- 图表类型：`bar` (期望 `bar`)
- 备注：`tool_calls=['list_datasources', 'query_engine', 'query_datasource', 'render_chart'] has_error=True`

### Q025 - 哪些天的销售额低于平均水平的 50%

- 类别：`anomaly`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `scatter`)
- 备注：`expected=scatter actual=bar`
