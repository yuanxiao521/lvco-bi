# Agent 评测 Baseline 报告

**生成时间**：2026-08-18 15:59:38
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：302.6s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 12 / 25 | **48.0%** |
| 工具调用成功率 | 19 / 25 | **76.0%** |
| 输出有效率 | 23 / 25 | **92.0%** |
| 图表类型正确率 | 15 / 25 | **60.0%** |
| 图表配置合法率 | 19 / 25 | **76.0%** |
| 平均迭代轮数 | 2.56 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 1 | 2 | 2 |
| anomaly | 2 | 1 | 1 | 1 |
| comparison | 4 | 1 | 1 | 4 |
| distribution | 5 | 3 | 5 | 5 |
| kpi | 4 | 2 | 3 | 4 |
| ranking | 3 | 2 | 2 | 2 |
| trend | 5 | 2 | 5 | 5 |

## 失败案例（top 10）

### Q007 - 客户年龄段分布

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `bar`)

### Q009 - 独立客户数有多少？

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `kpi_card`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q011 - 各销售渠道的销售额对比

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `grouped_bar`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

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

### Q016 - 复购率（同一客户下多单的占比）

- 类别：`aggregation`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`kpi_card` (期望 `kpi_card`)

### Q018 - 最近 6 个月的销售走势

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`line` (期望 `line`)

### Q019 - 华东区各产品的销售额对比

- 类别：`comparison`
- SQL 准确：❌
- 工具成功：❌
- 输出有效：✅
- 图表类型：`` (期望 `horizontal_bar`)
- 备注：`tool_calls=['list_datasources'] has_error=False`

### Q020 - 本月销售额对比上月增长率

- 类别：`kpi`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`grouped_bar` (期望 `kpi_card`)
- 备注：`expected=kpi_card actual=grouped_bar`

### Q022 - 每天的退款金额

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`line` (期望 `line`)
