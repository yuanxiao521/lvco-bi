# Agent 评测 Baseline 报告

**生成时间**：2026-08-20 15:05:53
**数据集**：`dataset.jsonl`（33 道题）
**总耗时**：0.2s

## 核心指标

| 指标 | 数值 | 通过率 |
|---|---|---|
| SQL 准确率 | 25 / 33 | **75.8%** |
| 工具调用成功率 | 25 / 33 | **75.8%** |
| 输出有效率 | 3 / 33 | **9.1%** |
| 图表类型正确率 | 25 / 33 | **75.8%** |
| 图表配置合法率 | 25 / 33 | **75.8%** |
| 平均迭代轮数 | 3.3 | - |

## 分类分布

| 类别 | 总数 | SQL 准确 | 工具成功 | 输出有效 |
|---|---|---|---|---|
| aggregation | 2 | 2 | 2 | 1 |
| anomaly | 2 | 2 | 2 | 0 |
| canvas | 8 | 0 | 0 | 0 |
| comparison | 4 | 4 | 4 | 0 |
| distribution | 5 | 5 | 5 | 0 |
| kpi | 4 | 4 | 4 | 2 |
| ranking | 3 | 3 | 3 | 0 |
| trend | 5 | 5 | 5 | 0 |

## 画布专项

画布题共 **8** 道，覆盖多图表 / 叙事 / 布局三维度。

| 子项 | 含义 | 通过率 |
|---|---|---|
| canvas_score | 5 子项全部通过 | **100.0%** |
| chart_count | 实际图表数 ≥ 期望最小图表数 | 100.0% |
| chart_types | 实际图表类型 ≥ 50% 覆盖期望集合 | 100.0% |
| narrative | 含 h1/h2/text 叙事块 | 100.0% |
| arrange_layout | arrange_layout 出现在末尾 1/3 窗口 | 100.0% |
| block_order | 叙事块先于最早图表块 | 100.0% |

### 画布题明细

| 题目 | 期望类型 | 实际类型 | 图表数 | 通过子项 | 总分 |
|---|---|---|---|---|---|
| Q026 | bar,pie | bar,pie | 2 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q027 | line,bar | line,bar | 2 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q028 | bar,line,pie | bar,line,pie | 3 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q029 | pie,pie,bar | pie,pie,bar | 3 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q030 | grouped_bar,line | grouped_bar,line | 2 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q031 | bar,line,pie,area | bar,line,pie,area | 4 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q032 | bar,bar | bar,bar | 2 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |
| Q033 | line,scatter | line,scatter | 2 | count✓ types✓ narr✓ arr✓ order✓ | ✅ |

## 失败案例（top 10）

### Q002 - 最近 30 天的销售额趋势

- 类别：`trend`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=12 keywords_missing=['每天']`

### Q003 - 各地区的销售额占比

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`pie` (期望 `pie`)
- 备注：`response_len=12 keywords_missing=['地区']`

### Q004 - 销售额前 10 的产品

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=13 keywords_missing=['产品']`

### Q005 - 对比华东和华北区本月的销售额

- 类别：`comparison`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`grouped_bar` (期望 `grouped_bar`)
- 备注：`response_len=12 keywords_missing=['华北', '对比']`

### Q006 - 每个月的平均订单金额

- 类别：`aggregation`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=11 keywords_missing=['平均']`

### Q007 - 客户年龄段分布

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=12 keywords_missing=['客户']`

### Q008 - 最近 7 天每天的订单数

- 类别：`trend`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`line` (期望 `line`)
- 备注：`response_len=13 keywords_missing=['7天', '每天']`

### Q010 - 客单价最高的 5 个产品类别

- 类别：`ranking`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=13 keywords_missing=['类别']`

### Q011 - 各销售渠道的销售额对比

- 类别：`comparison`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`grouped_bar` (期望 `grouped_bar`)
- 备注：`response_len=12 keywords_missing=['对比']`

### Q012 - 订单金额分布（按金额段分组）

- 类别：`distribution`
- SQL 准确：✅
- 工具成功：✅
- 输出有效：❌
- 图表类型：`bar` (期望 `bar`)
- 备注：`response_len=12 keywords_missing=['分布']`
