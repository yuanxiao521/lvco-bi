# Lvco BI 代码 Bug 排查清单

> 用途：按模块逐项人工复核 + 结合自动化脚本定位剩余 bug。
> 状态说明：`[已修]`=本会话已修复；`[待查]`=已定位待处理；`[建议]`=功能缺口/隐忧。

## 0. 自动化排查入口（先跑一遍）

| 检查项 | 命令 | 最近结果 |
|--------|------|----------|
| 后端单元/集成测试 | `python -m pytest tests -q` (backend) | 583 passed |
| 前端类型检查 | `npm run typecheck` (frontend) | 已修复，0 error |
| API 冒烟排查 | `python scripts/smoke_core_flow.py` (需后端运行) | 待运行 |
| 数据源健康 | `python scripts/quick_check.py` | 待运行 |

**重要**：测试全绿 ≠ 无 bug。测试只覆盖已写的用例，前端真实交互和 API 层字段往往不在覆盖内。

---

## 1. 全局：字段命名一致性（高危）

本会话类型检查一口气暴露了多处 **snake_case / camelCase 混用**，这是本项目复现率最高的 bug 源，原因就是前后端一方的字段名对不上另一方。

- [已修] `FreeCanvas/index.tsx` 拖入指标时传 `formula_type`/`depends_on_metric_ids`/`datasource_id` → 改为 `formulaType`/`dependsOnMetricIds`/`datasourceId`
- [已修] `types/metric.ts` 的 `MetricUpdatePayload` 已按后端 `MetricUpdate` 字段对齐为 camelCase（去掉了后端不接受的 `key/formula_type/depends_on_metric_ids`）。**注意**：后端 `MetricUpdate` 本身不含公式类型字段，改公式时类型由后端重新推断（见 §3）。
- [建议] `MetricImpact/MetricLineageEdge/MetricVersion*` 仍是 snake_case，但 `MetricDefinition` 是 camelCase。前端消费这些接口时若按 snake_case 读，逻辑能跑但很脆弱。建议统一。

**通用排查法**：改任意实体时，`grep` 该实体在 `types/`、`api/*.ts`、页面里的两个拼写，确认前后端对齐。

---

## 2. 本会话已定位并修复（前端类型错误，13 处 → 0）

| 文件 | 问题 | 处置 |
|------|------|------|
| `pages/DataSource/index.tsx` | `AlertTriangle` 未导入，连接失败分支会编译报错 | [已修] 补 import |
| `pages/Dashboard/Detail.tsx` | `Activity` 未使用 import；KPI 图标 `Icon` 类型不含 `style`，颜色不生效 | [已修] 删无用 import + 拓宽类型 |
| `pages/Login/index.tsx` | 错误解析类型把 `error` 放在 `response` 而非 `response.data`，`.message` 取不到 | [已修] 移到 `data` 下 |
| `pages/Share/index.tsx` | `blocks` 类型只允许嵌套对象，实际返回数组，类型与运行时不符 | [已修] 允许数组或嵌套 |
| `pages/AIChat/index.tsx` | 死变量 `collectedCharts` 声明了又在 `done` 里写，但从不读 | [已修] 删除声明与赋值 |
| `components/blocks/ConfigPanel` / `FieldPanel` | `onRemoveFilter`/`onAddFilter` 解构后从未使用 | [已修] 从解构中移除（见 §6 功能缺口） |

---

## 3. 指标中心（Metric Center）— 历史痛点，重点排查

[待查/P2] 用户反馈"指标中心难用、阈值高、新增指标不能直接进画布"：

- [已修] `FieldPanel` 增加 **"指标" tab**（无需先选数据源即可查看），列出指标中心**全部**指标并标注**已绑定/未绑定**状态徽标，点击/拖拽即可加入画布。之前在"字段"视图里只展示绑定到当前数据源的指标，未绑定/新建指标根本看不见 —— 这正是"新增指标不能直接进画布"的根因之一。
- [已修/P1] 派生指标类型推断（见下）。
- [已修] **创建弹窗移除两个"白选"控件**：原"类型（基础/派生）"和"依赖指标 ID"手填项后端 `MetricCreate` 根本没有对应字段（`extra=ignore`，提交即丢弃），会误导用户。现改为按公式自动识别并展示"基础/派生"类型提示（与后端推断逻辑一致）。
- [已确认] "免写 SQL 生成器"已存在：简单模式选「数据源+字段+聚合」→ 后端 `create_metric` 用 `source_field+agg` 自动生成 `SUM("field")` 并回填类型/依赖。**无需另造**。
- [已确认] 指标中心列表页已标注绑定/模板状态徽标（"已绑定数据源"/"模板指标"）。

[已修/P1] **派生指标创建后类型仍为 basic**（功能正确性）：后端 `create_metric`/`update_metric` 原来从不设置 `formula_type`/`depends_on_metric_ids`（DB 默认 `basic`），而 `query_engine` 靠 `formula_type=='derived'` 路由执行 → UI 建的派生指标没走对逻辑。现抽出 `_infer_metric_meta()`，按公式里 `metric('key')` 引用推断类型并回填依赖 ID（创建与改公式时都重新推断）；新增 4 个单元测试。前端 `MetricUpdatePayload` 已对齐（见 §1）。

[已修/P1] **详情页四个按钮"按了没反应"**（用户反馈）——根因是详情页三个数据接口的返回契约与前端对不上，导致血缘/影响/版本全是空或崩：
- `/dependencies` 原返回依赖指标的 **id 字符串列表**，前端当 `MetricDefinition[]` 用 → 上游节点空白/边指向无效 id → 血缘图崩塌。
- `/dependents` 原返回 **MetricUsage 记录**，前端当 `MetricDefinition[]` 用 → 下游节点空白。
- `GET /{metric_id}/versions` **接口根本不存在** → 版本治理 tab 永远空、对比也无可选版本。
- 处置：新增 `_metric_dict()` 辅助函数统一输出前端需要的 camelCase 序列化；`/dependencies` 现返回解析后的指标对象，`/dependents` 现按 `depends_on_metric_ids` 反查引用它的指标对象，新增 `GET /{metric_id}/versions` 列表接口（升序）。更新/新增 5 个单元测试对齐新契约。
- 另：回滚弹窗 placeholder 由"如 v1"改"如 2"（版本是整数，照旧填会 422），且 `runModal` 补了 try/catch 与弹窗内错误提示，失败不再被吞成"没反应"。

---

## 4. 画布（FreeCanvas）

- [已修/P2] 过滤器无法删除：`ConfigPanel` 现在渲染"过滤条件"区并显示删除按钮（复用 `DropZone`），删除按钮接回 `onRemoveFilter`。`FieldPanel.onAddFilter` 确认为死回调（真正加筛选是"拖时间字段"），已移除解构，不再补 UI。
- [已修/P1] 度量/指标字段名 camelCase 统一（见 §1），拖入指标的类型/口径展示不再丢失。

---

## 5. 图表渲染（ChartRenderer）— 建议回归验证

本项目有大量已沉淀的图表约束，改动渲染逻辑时务必对照：

- [建议] 双度量图表必须开双 Y 轴，右侧轴 `position:'right'` 且轴色随系列。
- [建议] grid `bottom≥36 / top≥40`，分类轴 >6 项旋转 30°。
- [建议] 横条图按数值降序、顶上显示。
- [建议] 图例可点击显隐、可滚动；图例图标统一圆角。
- [建议] 热力图要单独传 xFields/yFields，轴名显示维度字段名。
- [待查] PDF 导出时图表块必须带 `_chartResult`+`_chartConfig`，否则空白。

---

## 6. 功能缺口 / 半成品（排查时顺带确认）

- [已修/P2] 配置面板无法删除筛选：已补"过滤条件"区并接回 `onRemoveFilter`。
- [已修/P3] 字段面板 `onAddFilter` 死回调已清理（加筛选走"拖时间字段"，无需单独入口）。
- [已修/P2] 过滤条件新增编辑 UI：`ConfigPanel` 里每条筛选渲染 `FilterEditor`，可切换比较符（等于/不等于/大于/大于等于/小于/小于等于/介于/包含于/模糊）并编辑值（含 between 区间两输入、in 逗号分隔多值）。输入用本地 state、失焦提交避免光标跳动。后端 `query_engine._build_where` 已支持全部 9 种操作符。

---

## 7. 后端（已过 587 测试，重点看 API 契约）

- [待查] 用 `smoke_core_flow.py` 过一遍核心链路，特别看 `/api/v1/datasources`、`/statistics/describe` 在有数据源时是否 200（此前有端口/连接类问题历史）。
- [建议] SQL 内部错误不得透传用户；执行失败时不套用图表配置。
- [建议] `query_datasource` 工具 FROM 用 `table_ref`、列名用 `list_datasources` 返回 + 双引号。

---

## 8. 推荐排查节奏

1. 先跑 §0 的三个脚本（测试 / 类型 / 冒烟）；
2. 从 §3 指标中心 + §4 画布这两个用户直接用的模块开始手测（它们也是简历亮点，必须稳）；
3. 用 §1 的命名一致性 grep 法扫所有实体；
4. 每个修复后补一个断言/单测，防止回归。

> 备注：这份清单会随排查不断更新；修复时要遵循既有硬约束（async/await、路由文件不改、图表约束等，见项目 memory）。