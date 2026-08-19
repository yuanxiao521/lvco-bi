# Agent 评测 Baseline 报告

**生成时间**：2026-08-18 23:01:53
**数据集**：`dataset.jsonl`（25 道题）
**总耗时**：337.8s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 22 / 25 | **88.0%** |
| 工具调用成功率 | 25 / 25 | **100.0%** |
| 输出有效率 | 24 / 25 | **96.0%** |
| 图表类型正确率 | 17 / 25 | **68.0%** |
| 图表配置合法率 | 25 / 25 | **100.0%** |
| 平均迭代轮数 | 3.16 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 2 | 2 | 2 |
| anomaly | 2 | 1 | 2 | 2 |
| comparison | 4 | 4 | 4 | 4 |
| distribution | 5 | 4 | 5 | 5 |
| kpi | 4 | 4 | 4 | 4 |
| ranking | 3 | 3 | 3 | 3 |
| trend | 5 | 4 | 5 | 4 |

## 失败案例（top 10）

### Q012 - 订单金额分布（按金额段分组）

- 类别：`distribution`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `bar`)
- 备注：`result_set_mismatch: expected_rows=4 actual_rows=4`

### Q013 - 本季度销售额与上季度对比

- 类别：`trend`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：❌
- 图表类型：`grouped_bar` (期望 `grouped_bar`)
- 备注：`result_set_mismatch: expected_rows=3 actual_rows=2; response_len=410 keywords_missing=['环比']`

### Q025 - 哪些天的销售额低于平均水平的 50%

- 类别：`anomaly`
- SQL 准确：❌
- 工具成功：✅
- 输出有效：✅
- 图表类型：`bar` (期望 `scatter`)
- 备注：`result_set_mismatch: expected_rows=81 actual_rows=1; expected=scatter actual=bar`
