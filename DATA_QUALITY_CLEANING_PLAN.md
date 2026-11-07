# 数据体检 + 清洗：实施方案（对话助手 = 数据分析师）

> 目标：把「数据能不能用」和「脏数据怎么变成可分析的数据」这两件事，做成对话助手的一等能力。
> 本文只覆盖体检与清洗，不含画布改造；画布助手继续负责可视化编排。

## 0. 一页结论

- **体检**：新增 `inspect_dataset` 观察工具 + REST 接口，输出结构化质量报告（分数/问题/严重度/样例/建议），对话里以卡片展示。
- **清洗**：用户描述目标 → Agent 生成**清洗计划**（算子列表）→ 干跑（只统计影响行数）→ 用户确认 → 执行生成**派生数据源**（原表不动）→ 自动前后对比。
- **数据模型只加两样**：`SourceType` 增加 `derived`、`datasources.derived_from_id`；新增 `data_transformations` 表留痕。
- **不违反既有硬边界**：Lead 仍然没有写工具。清洗执行放在执行层（executor）+ 复用现有 HITL confirm 闸。
- 预估工作量：M1 体检 2–3 天 / M2 计划+干跑 3–4 天 / M3 执行+派生 3–4 天，合计约 2 周。

## 1. 现状与缺口（为什么要做）

| 现在有什么 | 缺什么 |
|---|---|
| `data_quality.py` 已有 5 个检测器：空值、异常值（阈值/IQR）、格式、重复行、类型不一致 | 只能通过 REST `/datasources/{id}/ai-clean` 或 Statistics 页按钮触发，对话里不可用 |
| `DataQualityTool` / `CleanSuggestTool`（LLM 给建议）已注册为 `orchestrator_safe` | 只能"建议"，**不能执行**；没有计划、没有确认、没有前后对比 |
| DuckDB 写窗口 `duckdb_client.write_window()` 已具备 | 没有任何"落一张新表"的产品语义 |
| HITL 确认链路（`confirm_request` → 前端卡片 → `ui_action=confirm`）已跑通 | 清洗这类破坏性操作还没接进来 |

结论：**地基都在，缺的是"把体检/清洗变成可执行、可确认、可追溯的产品动作"。**

## 2. 关键设计决策（请先拍这四个）

### 决策 1：清洗结果落成「派生数据源」，不覆盖原表（推荐）

- 原表只读、可回溯；派生源命名 `原名 · 清洗后`，列表里带「派生」徽章；
- 覆盖原表不做（DuckDB 里 CSV 源是本地表，可以覆盖，但不可逆、指标口径会漂、审计说不清）；
- 如果业务确实要"就地修正"，留一个后置选项：`apply(target=in_place)` + 双确认 + 备份快照，本期不做。

### 决策 2：清洗执行在执行层，不在 Lead

我们的硬边界是「Lead 没有写工具」（`lead_agent_tools.WRITE_TOOL_NAMES` 与真实写工具对齐，由测试断言）。
清洗会写数据，所以：

- Lead 只有观察权：`inspect_dataset`、`query_sample`；
- 执行走既有 `dispatch_analysis(goal="清洗…")` → chat worker（orchestrator）；
- 写动作由工具 `apply_cleaning` 完成，且**必须过 confirm**（复用现有 HITL）。

### 决策 3：大表上限与性能预期

- `CREATE TABLE AS SELECT` 在 DuckDB 里是流式的，但派生数据源会占磁盘（`data/duckdb`）；
- 默认上限：源行数 ≤ 5,000,000 且预估产物 ≤ 2GB，超出则拒绝并提示（可配置）；
- 每次清洗执行有超时（默认 120s）与写窗口独占，期间其它导入排队。

### 决策 4：敏感列交互规则

- 已标记 `sensitive` 的列：**禁止**参与 `fill_missing` / `derive` / `cast` 等会产生可读数据的算子；
- 只允许 `drop_column`（移除敏感列）——这本身就是治理动作；
- 派生数据源继承敏感标记，不复用样例值。

## 3. 数据模型

### 3.1 `SourceType` 增加 `derived`

```python
class SourceType(str, enum.Enum):
    csv = "csv"
    excel = "excel"
    mysql = "mysql"
    postgresql = "postgresql"
    derived = "derived"   # 新增：由清洗/加工产生，只存在于 DuckDB
```

为什么用新枚举值而不是加 `kind` 字段：派生源的查询形态**和 CSV 一样**（`"schema"."data"` 本地表），
复用现有全部链路（查询引擎、预览、画布、工具层、指标），零改动；加字段则要在每个分支判 kind，容易漏。

### 3.2 `datasources` 增加来源字段

```python
derived_from_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True), ForeignKey("datasources.id", ondelete="SET NULL"), nullable=True, index=True
)
```

派生源特征：`source_type=derived`、`file_path=None`、`connection_id=None`、
`schema_meta` 里记录 `{"table_name":"data","duckdb_schema":"orders_clean_ab12cd34","quality":{...}}`。

### 3.3 新增 `data_transformations`（清洗留痕）

```sql
CREATE TABLE data_transformations (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  source_datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,
  result_datasource_id UUID REFERENCES datasources(id) ON DELETE SET NULL,
  status VARCHAR(16) NOT NULL,         -- proposed | applied | failed | reverted
  rationale TEXT,                      -- 为什么这么洗（LLM 或用户给的理由）
  plan JSONB NOT NULL,                 -- {"operations":[...]}
  dry_run JSONB,                       -- 干跑结果：影响行数/比例/警告
  before_stats JSONB,                  -- 行数 + 质量报告快照
  after_stats JSONB,
  error VARCHAR(500),
  duration_ms INTEGER,
  confirmed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ
);
```

留痕的意义：**任何一个派生源都能回答"它从哪来、经过哪些操作、当时多少行、谁确认的"**——
这是分析可信度的底盘，也是审计要的东西。

## 4. 体检：报告结构与严重度

### 4.1 报告 JSON

```json
{
  "datasource_id": "uuid",
  "checked_at": "2026-11-05T10:00:00+08:00",
  "row_count": 12000,
  "column_count": 14,
  "score": 72,
  "grade": "B",
  "issues": [
    {"field": "order_date", "type": "type_inconsistency", "severity": "high",
     "count": 240, "ratio": 0.02, "samples": ["2024/13/01", "N/A"],
     "message": "2% 的日期无法解析为 DATE",
     "suggestion": "cast(order_date → DATE, on_error=null)"},
    {"field": "region", "type": "missing", "severity": "medium",
     "count": 360, "ratio": 0.03, "samples": [null],
     "message": "3% 的地区为空", "suggestion": "fill_missing(region, constant='未知')"}
  ],
  "column_profiles": [
    {"name": "total_amount", "type": "DOUBLE", "null_ratio": 0.0,
     "distinct": 1180, "min": 0, "max": 98500, "p50": 210, "p99": 8900,
     "outlier_ratio": 0.012, "sensitive": false}
  ],
  "notes": ["检测基于前 N 行抽样（N=50000）"]
}
```

### 4.2 严重度规则（代码判定，不让 LLM 决定）

| 类型 | high | medium | low |
|---|---|---|---|
| 空值率 | >30% | 10–30% | 1–10% |
| 重复行率 | >5% | 1–5% | 0–1% |
| 类型不一致 | >1% | 0.1–1% | <0.1% |
| 数值异常值 | >5% | 1–5% | <1% |
| 主键为空 | 任意 | – | – |

评分：从 100 分起，按 `severity × ratio` 扣分，钳制到 [0,100]；`grade` 90+ A / 75+ B / 60+ C / 40+ D / 其余 F。
**评分是代码算的**，LLM 只负责把结论说人话（避免"看起来很专业"的幻觉数字）。

### 4.3 采样与缓存

- 表 ≤ 50 万行：全量统计；> 50 万行：`USING SAMPLE 50000 ROWS`（并在 `notes` 里说明，不假装全量）；
- 结果写 `datasources.schema_meta.quality`（带 `checked_at`），Redis 缓存 10 分钟；
- 数据变更（重传/重同步）时失效。

## 5. 清洗：算子清单与编译规则

### 5.1 算子（第一期 9 个）

| 算子 | 参数 | 语义 | 破坏性 |
|---|---|---|---|
| `drop_duplicates` | `keys[]`, `keep=first/last` | 去重 | 是（删行） |
| `fill_missing` | `field`, `strategy=constant/mean/median/mode/ffill`, `value` | 补空值 | 否 |
| `drop_missing` | `field`, `min_non_null_ratio` | 删空值行 | 是 |
| `cast` | `field`, `to=INTEGER/DOUBLE/DATE/TIMESTAMP/VARCHAR`, `on_error=null/fail` | 类型转换 | 否 |
| `normalize_text` | `field`, `trim/lower/upper` | 文本清洗 | 否 |
| `clip_outliers` | `field`, `method=iqr/zscore/percentile`, `action=cap/remove/flag` | 异常值处理 | cap/remove 视为是 |
| `filter_rows` | `condition`（受白名单限制） | 行过滤 | 是 |
| `select_columns` | `fields[]` | 选列 | 否（但丢列） |
| `derive_column` | `field`, `expression`（受限表达式） | 派生列 | 否 |

（`rename_column` / `drop_column` 作为第 10、11 个，第二期加。）

### 5.2 计划 JSON

```json
{
  "rationale": "订单表有 2% 重复订单号、3% 地区为空、日期列混有非法格式",
  "operations": [
    {"op": "drop_duplicates", "keys": ["order_id"], "keep": "first"},
    {"op": "fill_missing", "field": "region", "strategy": "constant", "value": "未知"},
    {"op": "cast", "field": "order_date", "to": "DATE", "on_error": "null"}
  ],
  "expected_effect": {"drop_rows_estimate": 240, "fill_cells_estimate": 360}
}
```

### 5.3 编译为 SQL（一条 `CREATE TABLE AS SELECT`）

```sql
CREATE OR REPLACE TABLE "orders_clean_ab12cd34"."data" AS
WITH src AS (SELECT * FROM "orders_raw_9f8e7d6c"."data"),
     d1  AS (SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY "order_id" ORDER BY 1) AS __rn FROM src) WHERE __rn = 1)
SELECT
  * EXCLUDE (__rn),
  COALESCE("region", '未知') AS "region",
  TRY_CAST("order_date" AS DATE) AS "order_date"
FROM d1;
```

编译原则：
1. **每个算子生成一个显式步骤**（CTE 或列表达式），保证可读、可审计、可解释给用户；
2. 字段名一律双引号转义，**不接受用户/LLM 传入的原始 SQL 片段**；`derive_column` 的表达式只能引用已存在的列名 + 白名单函数（复用 `sql_guard_ast` 的函数白名单）；
3. 所有算子幂等（同一计划重复执行结果一致），执行前 `DROP TABLE IF EXISTS` 目标表。

### 5.4 干跑（dry-run）

执行前只统计、不写表：

- 影响行数：`SELECT COUNT(*) FROM <step_k> WHERE <会被该算子改变的条件>`；
- 空值填充数、类型失败数、重复数；
- 若「删行比例 > 30%」或「影响行数 > 总行数 50%」→ 标记 `requires_strong_confirm`。

## 6. 工具契约（如何接进对话助手）

### 6.1 Lead 层（观察，无写权限）

```python
_fn("inspect_dataset",
    "检查数据源质量：行数、空值、重复、类型不一致、异常值、敏感列，给出评分与清洗建议。",
    {"datasource_id": {"type": "string"}},
    ["datasource_id"])
```

与 `query_sample` 同一族；不写库、不改数据；每轮配额沿用观察类（2 次）。

### 6.2 执行层（chat worker 的工具）

| 工具 | 作用 | 是否写 | 闸门 |
|---|---|---|---|
| `propose_cleaning` | 依据体检报告 + 用户目标生成计划，返回干跑统计 | 否 | 敏感列/大表校验 |
| `apply_cleaning` | 执行计划，生成派生数据源，返回前后对比 | **是** | 必须带 `confirm_token`；`requires_strong_confirm` 需二次确认 |
| `verify_cleaning` | 重跑体检对比前后质量 | 否 | – |

### 6.3 一次完整对话时序

```
用户：帮我看下这份订单数据能不能用
  → Lead: list_datasources → inspect_dataset(ds)
  → 卡片【数据体检】评分 72/B，3 个问题（高/中/低）

用户：把重复订单去掉、地区补成"未知"，再分析
  → Lead: dispatch_analysis(goal="清洗订单表：去重+补地区，然后按地区分析销售额")
  → worker: propose_cleaning(...) → 卡片【清洗计划】4 个算子 + 干跑（影响 240 行 / 3.2%）
  → 用户点【确认执行】（复用现有 confirm_request / ui_action=confirm）
  → apply_cleaning(plan, confirm_token) → 生成派生源「订单表 · 清洗后」
  → 卡片【清洗结果】行数 12000 → 11760，重复率 2% → 0，空值 3% → 0
  → 继续在同一派生源上做分析（Lead 上下文自动切到新数据源）
```

最后一步很关键：**清洗后的分析必须落在派生源上**，否则用户会看到"洗了但数字没变"，这是最容易穿帮的地方。
Lead 需要在 dispatch 结果里拿到 `result_datasource_id` 并更新本轮上下文的默认数据源。

## 7. REST 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/datasources/{id}/quality` | 触发体检（`checks[]` 可选），返回报告 |
| GET | `/datasources/{id}/quality` | 读缓存报告（无则 404 提示先体检） |
| POST | `/datasources/{id}/cleanings` | 提交计划 → 干跑 → `status=proposed` |
| POST | `/cleanings/{id}/apply` | 执行（body 带 `confirm_token`）→ 派生数据源 |
| GET | `/cleanings/{id}` | 执行结果 / 前后对比 |
| POST | `/cleanings/{id}/revert` | 派生源进回收站（软删），转换记录置 `reverted` |

权限：全部要求 `datasource` 所有权；执行类限 `editor` 及以上；审计中间件已覆盖（只记路径，不记数据）。

## 8. 前端改动

1. **对话卡片**（新事件类型，走现有 SSE 事件通道）：`quality_report` / `cleaning_plan` / `cleaning_result`；
2. **确认交互**：复用画布确认卡片（`confirm_request` → `ui_action={type:"confirm"}`），无需新机制；
3. **数据源列表**：派生源显示「派生」徽章 + "来自：订单表"+ 点击跳转原源；
4. **详情弹窗**：新增「数据质量」区块（评分环 + 问题列表 + 一键"生成清洗计划"）；
5. **删除语义**：删派生源只删 DuckDB catalog 与数据源行，不动原表（`revert` 同）。

## 9. 实施计划（3 个里程碑）

### M1 体检（2–3 天）

- [ ] `data_quality.inspect()` 统一入口：汇总 5 个检测器 + 列画像 + 评分 + 严重度；
- [ ] 采样策略与 Redis 缓存、`schema_meta.quality` 落盘；
- [ ] REST 两个接口 + `inspect_dataset` 工具（Lead 观察）+ 事件 `quality_report`；
- [ ] 前端体检卡（对话）+ 详情「数据质量」区块；
- [ ] 验收：对 `orders`（CSV）与一个 PG 表各跑一次，报告数字与手工 SQL 对齐。

### M2 计划 + 干跑（3–4 天）

- [ ] 算子编译器（9 个算子 → SQL）+ 敏感列/大表守卫；
- [ ] `POST /datasources/{id}/cleanings`（干跑统计）+ 计划卡；
- [ ] `propose_cleaning` 工具（chat worker）；
- [ ] 验收：同一计划重复生成结果一致；干跑统计与实际执行差异 ≤ 1%。

### M3 执行 + 派生数据源（3–4 天）

- [ ] `SourceType.derived` 枚举迁移 + `derived_from_id` + `data_transformations` 表；
- [ ] `apply_cleaning`（写窗口 + 超时 + 派生源创建 + 前后体检）；
- [ ] confirm 闸接线 + 结果卡 + 派生徽章 + `revert`；
- [ ] 验收：执行后行数/质量变化与干跑一致；重新查询派生源能出图；原表未被修改（哈希/行数对比）。

### M4（可选，后续）

归因拆解、清洗模板库、清洗 pipeline 可视化、行级权限。

## 10. 测试与验收

**单元**（预计 +25 条）
- 每个算子编译出的 SQL 正确性（含引号/空表/全空列）；
- 守卫：敏感列拒绝、超行数拒绝、删行比例触发强确认；
- 评分与严重度边界（0/1%/30%/100%）。

**集成/真机**
- CSV 源：造一张含重复/空值/脏日期的表 → 体检 → 清洗 → 派生源查询；
- PG 源：同上，验证外部库不被写入（原表行数不变）；
- 对话链路：体检卡 → 计划卡 → 确认 → 结果卡 → 后续分析落在派生源。

**回归**：全量 `pytest`（当前 1136 passed）不得下降；前端 `tsc --noEmit` + `build`。

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 清洗把数据改坏且不可逆 | 高 | 只派生不覆盖；派生源可删重来；留痕可溯源 |
| LLM 生成的算子参数离谱（如全表删行） | 高 | 代码守卫 + 干跑阈值 + 强确认；参数 schema 校验 |
| 派生源膨胀占满磁盘 | 中 | 单源大小上限、派生源数量上限（默认 20/用户）、回收站定期清理 |
| 大表清洗超时/拖垮 DuckDB | 中 | 行数上限、写窗口独占、超时中断、失败清理半成品表 |
| 用户误以为原表已被修好 | 中 | 派生源命名+徽章+toast 明确"原表未改动" |
| 敏感列被清洗动作绕过 | 中 | 敏感列只允许 drop；派生源继承标记 |

## 12. 明确不做（本期）

- 不做覆盖原表（in-place）；
- 不做分布式/超大数据集清洗（> 500 万行直接提示导出处理）；
- 不做清洗模板库与定时重跑（M4）；
- 不做行级权限与多租户隔离；
- 不改画布助手的定位（画布继续只管可视化）。

## 13. 面试可以这么讲（三句话）

1. "很多 AI BI 停在‘能出图’，我们把**数据治理动作**做进了对话：体检报告是代码算的，清洗计划是模型拟的、代码干跑、用户确认、代码执行。"
2. "清洗结果永远落成派生数据源，链路可回溯——任何一张图都能回答‘这个数从哪张表、经过哪些清洗、谁确认的’。"
3. "边界很清楚：模型只提议，写动作在执行层且必须人工确认，所以不会出现‘Agent 偷偷把客户数据改了’这种事故。"

## 14. 待你确认的 4 个决策点

1. 派生数据源 vs 覆盖原表 → 建议派生（本文按派生设计）；
2. 清洗执行放执行层 + confirm → 建议是（守住 Lead 无写工具的不变量）；
3. 大表上限 500 万行 / 单源 2GB → 可调，按你目标数据规模定；
4. 敏感列只允许 drop → 建议是（否则敏感列可以借清洗"复活"成可读数据）。

## 15. 实现备注（已在本机验证过的技术前提）

1. **PG 枚举加值**：`source_type` 是 PG 原生枚举，迁移里用 `ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'derived'`。
   PG 12+ 允许在事务里加值，但**同一事务内不能使用**新值——所以该迁移只做"加值 + 建表"，
   不插入任何 `source_type='derived'` 的行（派生源在运行时创建，天然不在同一事务）。
2. **DuckDB 语法**（已实测可用）：`CREATE OR REPLACE TABLE ... AS`、`TRY_CAST`、`SELECT * EXCLUDE (col)`、
   `USING SAMPLE 50000 ROWS`、`ROW_NUMBER() OVER (PARTITION BY ...)`。
3. **写窗口**：`DUCKDB_READ_ONLY=true` 下落地派生表必须走 `duckdb_client.write_window()`（上传导入已在用同一套），
   否则写不进去；写窗口期间是全进程独占，注意执行超时要放在窗口内部计时。
4. **schema 命名**：派生源用 `<原 schema 前缀>_clean_<8位hash>`，与现有 `get_schema_name()` 规则一致，
   并同时写进 `schema_meta.duckdb_schema`，避免改名后找不到 catalog。
5. **行数统计口径**：体检的 `row_count` 用 `COUNT(*)`；派生前后对比必须用同一口径（含被过滤行），
   不要把"去重后行数"和"抽样行数"混在一起报。
6. **失败清理**：执行中途失败要 `DROP TABLE IF EXISTS` 目标 schema，并把 `data_transformations.status` 置 `failed`，
   不能让半成品表留在 DuckDB 里被发现。
