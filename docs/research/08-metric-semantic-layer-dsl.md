# 08 · 指标语义层：表达力边界与 DSL 设计

> **一句话结论**：主流语义层没有一家允许在「指标表达式」里写任意 SQL——它们开放的是**表/来源层**（derived table、cube-level sql、SQL source），而把「指标」收敛为一个**封闭的聚合规格 + 受限表达式**。这不是能力不足，而是治理、下推、缓存、权限、血缘五件事同时逼出来的架构选择。
>
> **证据说明（先声明，别被下面的 URL 误导）**：本轮执行环境**外网被阻断**——tools.web_fetch 对所有域名报 "resolves to a non-public IP address"（DNS 返回 198.18.0.255），pwsh 里 curl/Invoke-WebRequest 直连与走本地代理均 TLS 握手失败，web_search 只返回 URL 列表、无正文。因此本文**没有逐字复核任何官方页面**；所有引用都以 canonical URL 给出，内容来自模型对这些系统官方文档（主体是 2023–2025 版本）的记忆。
>
> 为避免把记忆当事实，全文用三级标注：
> - 【A】语义稳定、我高置信（如 LookML 有 measure 的 filters 参数、Cube 有 segments）
> - 【B】方向确定，但字段名/版本细节可能已变（如 dbt derived 的输入清单写法）
> - 【C·未证实】我不确定，**上线前必须复核**（文末有清单）
>
> 结论级别的话可以直接用；**代码级细节（字段名、参数名）一律按 B 对待**。

---

## §0 一页结论

| # | 结论 | 支撑 |
|---|---|---|
| 1 | 「指标」在语义层里是**规格（spec）而非查询**：`agg + expr + 维度 + 过滤器` 四件套，编译器负责生成 SQL。谁把 SQL 写进指标，谁就失去了编译器的全部好处。 | §1、§8 |
| 2 | **任意 SQL 的开放位置一律在「表/来源层」**：LookML 的 derived table、Cube 的 cube 级 sql、Malloy 的 SQL source、Metabase 的 native query。指标层只吃「列 + 行级表达式」。 | §3 |
| 3 | 条件口径有两条正交的路：**指标自带 filters（编译成条件聚合 CASE WHEN）** vs **查询级 WHERE/segment（下推过滤）**。前者不影响同查询的其它指标，后者影响全部——这是选型的第一个分水岭。 | §4 |
| 4 | 比率**普遍需要显式类型**（dbt 的 ratio、Looker 的 type: number、Cube 的 calculated measure），因为「先聚合再相除」和「先相除再聚合」结果不同，且分母必须独立聚合。占比（share of total）则**普遍不做进指标**，落在窗口函数或 BI 层。 | §5 |
| 5 | 派生指标的依赖图是**编译期资产**：dbt 用显式 metrics 输入清单 + DAG，Cube 用 measure 引用 + 多阶段查询，LookML 是隐式 measure 引用。环检测都在编译/校验期，不在运行时。 | §6 |
| 6 | 面向 LLM 的正确姿势不是「把 SQL 给模型」，而是**把指标清单 + 维度清单 + 受约束的查询工具给模型**（MCP 化）。模型产出的是「指标名 + group_by + filter」，SQL 由语义层编译。 | §7 |

**一句反直觉的话**：语义层的表达力上限，不是由「用户想算什么」决定的，而是由「编译器能不能证明这段表达式只做了一次聚合、只碰了已声明的表、能被下推和缓存、能被权限系统拦截」决定的。

---

## §1 先分范式：四种「指标」观

不理解范式，会觉得这六家的差异是「功能多少」；理解范式，才知道差异是**本体论**。

| 范式 | 代表 | 「指标」是什么 | SQL 开放度上限 | 典型失败模式 |
|---|---|---|---|---|
| **A. 聚合规格**（spec / 声明式） | dbt MetricFlow、Cube、LookML | 一个聚合声明：度量（agg + 列表达式）+ 时间维度 + 过滤器 | 指标内只有**行级表达式**；SQL 开放给表（derived table / sql 子查询） | 用户想算的东西超出规格 → 只能新造表或退回原生 SQL |
| **B. 表达式语言** | Malloy | 指标是可组合的**语言级表达式**（measure = 聚合表达式，可引用其它 measure） | 语言本身图灵不强，但有 join/nest/refine；SQL 作为 source 的**逃生门** | 学习曲线；生态工具少 |
| **C. 受治理的保存查询** | Metabase | 一条**保存下来的聚合查询**（1 个聚合 + breakout + filter） | 表达式是「查询构建器里的自定义列」，不是 SQL | 无法指标引用指标，口径复用靠人 |
| **D. 多维模型**（OLAP 血统） | AtScale | 维度 + 度量 + **计算成员（calculated measure）**，用 MDX/DAX 表达 | 计算成员里是 MDX/DAX 表达式，不是 SQL；SQL 由引擎按方言生成 | MDX 人才稀缺；语义与引擎绑定 |

> 补充：**Transform** 属于 A 范式（YAML metrics on dbt，2021–2023），详见 §2.8（含未证实说明）。

这个分类解释了后面所有现象：
- A 范式的系统**必须**有 measure 与 metric 两层（度量是物理聚合，指标是业务口径）——因为 measure 可复用、metric 可组合。
- B 范式不需要两层，因为表达式天然可嵌套。
- C 范式只有一层，所以复用只能靠「复制问题」。
- D 范式的计算成员等价于 B 的表达式，但宿主是 OLAP 引擎，所以能吃到引擎的层次/时间智能。

---

## §2 逐系统：schema 与表达力边界

### 2.0 主对比表（题面要求的四列）

| 系统 | 表达力边界 | 比率 / 占比方案 | 过滤方案 | 派生支持 |
|---|---|---|---|---|
| **dbt Semantic Layer / MetricFlow** | measure = agg（sum/avg/count/count_distinct/min/max/median/percentile/sum_boolean）+ **列级 expr**；metric 五种类型：simple / ratio / cumulative / derived / conversion。**用户不可写窗口、子查询、JOIN 逻辑**（JOIN 由 semantic model 的 entity 声明）。 | **ratio 是显式类型**（numerator / denominator，各自可带 filter）；**占比无原生**——需上层计算或 BI 层；cumulative 由 MetricFlow 用时间 spine + 窗口生成。 | ①metric 级 filter（Jinja：Dimension / TimeDimension）②saved query 的 where ③measure 级 filter【B】 | derived metric：expr 里对其它 metric 做四则，且需显式列出输入 metrics → 编译期 DAG【B】 |
| **Cube** | measure 的 sql 是**表达式**（可 CASE WHEN、任意标量函数），可引用其它 measure（calculated measure）、可跨 cube（需 join）；cube 级 sql 可以是**整段子查询**；measure 内**不可写 SELECT**。 | **calculated measure**：sql 里写 `{a} / NULLIF({b}, 0)`，引擎生成多阶段查询；总计用查询参数 total；占比靠多阶段或 BI 层。 | ①measure 级 filters（编译成条件聚合）②segments（命名布尔条件，查询级 WHERE）③query 级 filters | calculated measure 引用 measure（同 cube 直接引用，跨 cube 需 join），多阶段 SQL 生成【A/B】 |
| **LookML / Looker** | dimension / measure 的 sql 是**方言表达式**（CASE WHEN、任意函数、${其它字段} 引用）；measure 类型是**封闭枚举**（sum/average/count/count_distinct/min/max/median/percentile/sum_distinct/average_distinct/number/running_total…）；**measure 内不可写窗口/子查询**；派生表（derived table）是表级任意 SQL 的逃生门。 | **type: number** 里引用其它 measure 做四则（社区标准做法）；**占比靠 table calculation**（percent_of_total / pivot_index / offset），不进 LookML。 | ①measure 的 filters 参数（编译成条件聚合）②always_filter / sql_always_where ③access_filter（行级权限） | measure 引用构成隐式依赖；**symmetric aggregates** 会让「引用另一个 measure」在 fan-out 场景下必须小心【A/B】 |
| **Metabase** | metric = 保存的聚合查询（1 个聚合 + breakout + filter）；自定义表达式支持算术 / case / CountIf / SumIf 等；**无窗口函数**【C】。 | 无 ratio 类型；比率 = 两个 metric 相除（需 CTE / 原生 SQL / 可视化层）；占比只有图表展示层。 | ①segment（绑定表/模型的命名 filter）②查询级 filter | **基本没有**（metric 不能引用 metric）；新版 Library metric 是否支持引用【C·未证实】 |
| **AtScale** | 维度 + 度量 + **计算成员**（MDX，部分引擎 DAX）；支持层次、时间智能、聚合感知（aggregate awareness）；SQL 由引擎适配生成。 | 计算成员里写除法（IIF / NULLIF 处理 0）；占比靠计算成员或查询层。 | 维度切片 + 计算成员的集合过滤 + 行级安全【B】 | 计算成员引用其它 measure，构成依赖图【B】 |
| **Transform（transform.co）** | YAML metrics on dbt（A 范式），指标 = 聚合 + 过滤 + 时间粒度；以「metric catalog + SQL 生成」为核心。 | 支持 ratio 型指标（YAML 声明）【C】 | 指标级 filter【C】 | 支持指标依赖【C】 |
| **Malloy** | **语言级**：source / dimension / measure / view / nest / join_one / join_many / aggregate / refine；measure 是聚合表达式，可直接组合其它 measure；SQL 可作 source（逃生门）。 | 比率 = 直接写除法；占比需窗口或二次聚合【C】 | view / query 的 where、refine 覆盖 | measure 组合天然是表达式树（DAG），无显式依赖声明 |

> 表内【A/B/C】是证据等级，不是功能强弱。

---

### 2.1 dbt Semantic Layer / MetricFlow

**schema 三层**：semantic model（物理表 + entity + measure + dimension）→ metric（业务口径）→ saved query（导出/复用）。

~~~yaml
semantic_models:
  - name: orders
    model: ref('orders')
    defaults:
      agg_time_dimension: order_date
    entities:
      - name: order_id
        type: primary
      - name: customer
        type: foreign
        expr: customer_id
    measures:
      - name: revenue
        agg: sum
        expr: amount            # 列级表达式，可 CASE WHEN【B】
      - name: order_count
        agg: count
        expr: 1
      - name: p95_amount
        agg: percentile
        expr: amount
        agg_params: { percentile: 0.95 }   # 【B】
    dimensions:
      - name: order_date
        type: time
        type_params: { time_granularity: day }
      - name: is_food_order
        type: categorical
~~~

~~~yaml
metrics:
  - name: revenue
    type: simple
    type_params: { measure: revenue }

  - name: avg_order_value          # 客单价
    type: ratio
    type_params:
      numerator: revenue
      denominator: order_count

  - name: revenue_us               # 条件口径
    type: simple
    type_params: { measure: revenue }
    filter: |
      {{ Dimension('order__is_food_order') }} = true

  - name: rolling_7d_revenue
    type: cumulative
    type_params:
      measure: revenue
      cumulative_type_params: { window: 7 days }   # 或 grain_to_date: month

  - name: gross_profit
    type: derived
    type_params:
      expr: revenue - cost          # 只允许四则 + 括号
      metrics:
        - name: revenue
        - name: cost
~~~

**表达力边界（关键）**：
- 度量（measure）只能做**一次聚合**，且 expr 里**不能再套聚合**——这是 spec 的硬约束，防止「聚合套聚合」无法下推。【B】
- metric 之间**只能做四则**；derived 的 expr 不是 SQL，是一个只认 metric 名 + 数字 + 运算符的小文法。【B】
- **JOIN 不是 SQL，是 entity 声明**：语义层靠 entity 推断 fan-out 与去重路径。想「跨表」不是写 JOIN，而是声明 foreign/primary entity。
- 时间维度是**全局 metric_time**：所有指标共享一套时间粒度，才能在同一查询里对齐。
- cumulative 指标的窗口是**引擎生成的**（时间 spine + 窗口/自连接），不是用户写的窗口函数。这点极其重要：**语义层自己用窗口，但不让你写窗口**。【A】

**比率/占比**：ratio 是显式类型（分子分母各自独立聚合并可各带 filter）【B】；**占比没有原生类型**——官方给的路子是「用 metric_time 分组后在上层算」或 BI 层表计算【B】。

**出处**：<https://docs.getdbt.com/docs/build/semantic-models>、<https://docs.getdbt.com/docs/build/metrics-overview>、<https://docs.getdbt.com/docs/build/derived>、<https://docs.getdbt.com/docs/build/ratio>、<https://docs.getdbt.com/docs/build/cumulative>、<https://docs.getdbt.com/docs/build/conversion>、<https://docs.getdbt.com/reference/metric-properties>（2024–2025 版文档；【B】）。

> 注：早期（2022–2023）MetricFlow 用 Jinja 形式 `{{ Metric('x') }}` 引用指标，后来改成 metrics 输入清单 + 裸名 expr。**两种写法在社区文章里并存**，实现时要按目标 dbt 版本确认——这是本文最容易踩的坑之一。【B】

---

### 2.2 Cube

**schema**：cube（物理表或 sql 子查询 + join）→ dimension / measure / segment → view（对外暴露层）。

~~~yaml
cubes:
  - name: orders
    sql: >
      SELECT * FROM public.orders WHERE deleted_at IS NULL
    joins:
      - name: customers
        relationship: many_to_one
        sql: "{CUBE}.customer_id = {customers}.id"
    dimensions:
      - name: status
        sql: status
        type: string
      - name: is_paid
        sql: "CASE WHEN paid_at IS NOT NULL THEN 1 ELSE 0 END"   # 任意表达式
        type: number
      - name: created_at
        sql: created_at
        type: time
        granularities: [day, week, month, quarter, year]        # 时间粒度在这里
    measures:
      - name: count
        type: count
      - name: revenue
        type: sum
        sql: amount
      - name: paid_revenue
        type: sum
        sql: amount
        filters:                       # 条件口径：编译成条件聚合
          - sql: "{CUBE}.status = 'paid'"
      - name: aov                      # 计算度量
        type: number
        sql: "{revenue} / NULLIF({count}, 0)"
    segments:
      - name: only_paid
        sql: "{CUBE}.status = 'paid'"
~~~

**表达力边界**：
- measure 的 `sql` 是**表达式**，不是 SELECT：可以 CASE WHEN、可以调标量函数、可以引用 `{CUBE}.col` 与 `{other_cube.measure}`。【A】
- **跨 cube 引用需要 join**：计算度量引用别的 cube 的 measure 时，Cube 会检查能否 join，不能则报错（这是「语义层拒绝猜测」的典型）。【B】
- cube 级 `sql` 可以写**整段子查询**——这是 Cube 的「表级逃生门」。【A】
- **measure 内不能写窗口函数**；rolling window / cumulative 由 measure 参数（`rollingWindow`、`cumulative`）交给引擎生成。【B】
- 计算度量会触发**多阶段查询**（先算内层 measure 结果集，再在外层做四则）。这也是 Cube 能支持「比率」的技术底座。【B】

**比率/占比**：calculated measure 是标准解法；占比通常在 BI 层或额外查询（`total` 参数拿总计再算）【B】。

**出处**：<https://cube.dev/docs/product/data-modeling/concepts/measures>、<https://cube.dev/docs/product/data-modeling/concepts/calculated-members>、<https://cube.dev/docs/reference/data-model/segments>（2023–2025；文档站 2025 年迁到 docs.cube.dev；【B】）。

---

### 2.3 LookML / Looker

**schema**：view（表/派生表 + dimension + measure）→ explore（join）→ model。

~~~lookml
view: orders {
  sql_table_name: analytics.orders ;;

  dimension: is_paid {
    type: yesno
    sql: ${paid_at} IS NOT NULL ;;
  }

  dimension: status {
    type: string
    sql: ${TABLE}.status ;;
  }

  measure: total_revenue {
    type: sum
    sql: ${amount} ;;
  }

  measure: paid_revenue {
    type: sum
    sql: ${amount} ;;
    filters: [status: "paid"]        # 条件聚合
  }

  measure: order_count {
    type: count_distinct
    sql: ${order_id} ;;
    sql_distinct_key: ${order_id} ;;   # 去重键，处理 fan-out
  }

  measure: aov {
    type: number
    sql: ${total_revenue} / NULLIF(${order_count}, 0) ;;
  }

  measure: paid_ratio {
    type: number
    sql: ${paid_revenue} / NULLIF(${total_revenue}, 0) ;;
  }
}
~~~

**表达力边界**：
- dimension / measure 的 `sql` 是**目标方言的表达式**：CASE WHEN、日期函数、正则、${其它字段} 全都可以。【A】
- 但 measure 的**类型是封闭枚举**——`type` 决定外层聚合函数是什么，`sql` 只决定参数。【A】
- **measure 里不能写窗口函数、不能写子查询**（会破坏对称聚合与外连接语义）【B】。窗口需求走 table calculation。
- **派生表（derived table）是表级任意 SQL**：`derived_table: { sql: SELECT ... ;; }`，这是 LookML 保留的完整逃生门。【A】
- **symmetric aggregates** 是 Looker 的核心正确性机制：当 explore 里有一对多 join 时，它把 measure 包进子查询去重后再聚合。**这直接意味着**：任何「手写聚合表达式」如果绕开这个机制，就会重复计数。这是「为什么不敢放开任意 SQL」最硬的技术理由。【A】

**比率/占比**：比率 = `type: number` 引用其它 measure（社区事实标准）；占比 = **table calculation**（percent_of_total / pivot_index / offset / running_total），不进 LookML。【A/B】

**出处**：<https://cloud.google.com/looker/docs/reference/param-measure-types>、<https://cloud.google.com/looker/docs/reference/param-field-sql>、<https://cloud.google.com/looker/docs/reference/param-field-filters>、<https://cloud.google.com/looker/docs/derived-tables>、<https://cloud.google.com/looker/docs/symmetric-aggregates>（Google Cloud 文档，2023–2025；【A/B】）。

---

### 2.4 Metabase

**schema**：table → model（保存的问题，可加列/类型）→ segment（命名 filter）→ metric（保存的聚合）。

- **metric** = 「Summarize 之后保存」的产物：一个聚合 + 若干 breakout + 若干 filter。它没有独立的表达式语法。【B】
- **segment** = 绑定在表/模型上的命名过滤条件，等价于「可复用的 WHERE 片段」。它**不含聚合**。【A】
- **custom expression** 是查询构建器里的表达式：支持算术、`case`、`coalesce`、`CountIf` / `SumIf`（这就是它的条件口径写法）【A】；窗口函数在构建器里**不支持**【C·未证实，v50+ 是否放开需查】。

**表达力边界**：本质是「可视化构建器能表达的东西」。想在 metric 里做复杂口径，唯一出路是**原生 SQL 问题**——而原生 SQL 问题**不是 metric**，不进入语义层，也就拿不到列级权限/血缘。【B】

**比率/占比**：无 ratio 类型；客单价要么写原生 SQL，要么建两个 metric 在可视化里算；占比只有图表展示层的百分比。【C】

**出处**：<https://www.metabase.com/docs/latest/data-modeling/metrics>、<https://www.metabase.com/docs/latest/data-modeling/segments>、<https://www.metabase.com/docs/latest/questions/query-builder/expressions>（2024–2025；【B/C】）。

> **对我们的直接启发**：Metabase 是「最小可用语义层」的下界样本——它证明了**只做 segment + 保存聚合**也能卖，但代价是「比率/占比/派生」全部外溢到人肉 SQL。我们仓库若想避免外溢，就必须在 formula 层补 ratio 与条件聚合。

---

### 2.5 AtScale

**定位**：不存数据的「universal semantic layer」，把维度模型映射到既有数仓（Snowflake/BigQuery/Databricks/Redshift…），查询时**翻译成引擎原生 SQL 或 MDX/DAX**。

- 建模对象：dataset / dimension / measure / **calculated measure（计算成员）** / hierarchy。【B】
- 计算成员用 **MDX**（部分引擎 DAX）表达：支持时间智能（YTD、ParallelPeriod）、集合运算、层次导航。【B】
- **Aggregate Awareness**：自动把查询改写到预聚合表上——这要求它**完全掌握查询结构**，任意 SQL 会直接摧毁这个能力。【B/A】
- 2024 起推 **SML（AtScale Modeling Language）**，YAML 化的模型定义【B】。

**出处**：<https://www.atscale.com/>、<https://docs.atscale.com/>（具体路径未逐字核对；【B/C】）。

---

### 2.6 Malloy

**定位**：Looker 创始人 Lloyd Tabb 主导的开源**语义建模语言**（malloydata/malloy），编译到 BigQuery / DuckDB / Postgres / Snowflake / Trino / Databricks。

~~~malloy
source: orders is table('analytics.orders') {
  join_one: customers is table('analytics.customers') on customer_id = customers.id
  dimension: is_paid is paid_at != null
  measure: revenue is sum(amount)
  measure: order_count is count()
  measure: aov is revenue / nullif(order_count, 0)
  view: by_status is {
    group_by: status
    aggregate: revenue, order_count, aov
  }
}
~~~

**表达力边界（与 A 范式的根本差异）**：
- measure 是**语言级表达式**，可以引用其它 measure（`aov` 引用 `revenue` / `order_count`），编译器负责把引用展开为「同一层聚合」。**不需要显式的 ratio 类型**——这是 B 范式相对 A 范式的最大优势。【B】
- 有 `nest:` / `aggregate:` / `refine`，可以做「明细 + 汇总同查」，这在 A 范式里很难表达。【B】
- SQL 是**source 层逃生门**（用 SQL 语句定义 source）【C·语法待核】。
- 有分析函数（lag/lead 类）能力，具体清单【C·待核】。

**出处**：<https://www.malloydata.dev/>、<https://github.com/malloydata/malloy>（2023–2025；【B/C】）。

---

### 2.7 单列一条：为什么 Malloy 值得单独看

它是唯一把「指标」做成**语言原语**而不是「配置项」的主流实现。如果我们的目标是「让 AI 生成指标口径」，语言比 YAML 更抗歧义（有作用域、有类型、有编译错误）；但代价是**模型必须学会一门语言**，且生态/工具链要自建。这是一个真实的战略分叉，不是功能对比。

---

### 2.8 Transform（transform.co）——含未证实说明

- Transform 是 2021–2023 年间的「metrics layer」厂商，产品形态是 **YAML 指标定义 + 在 dbt 模型之上生成 SQL + metric catalog**，属 A 范式。【B】
- 据公开报道，**Transform 于 2023 年被 Google 收购**，团队并入 Looker 相关方向【C·未证实，本轮无法复核】。
- 题面提到的「现 Transform AI」：**我无法确认**这是同一实体的新品牌、还是同名不同产品。**本文不对其现状做任何断言**。【C·未证实】

> 处理建议：在我们自己的选型文档里，**不要把 Transform 当作可选方案**，除非能拿到 2024 年之后的官方材料。它的历史价值在于：它最早把「指标 = YAML 声明 + 编译器生成 SQL」这件事产品化，和 dbt MetricFlow 是同一路线。

---

## §3 核心问题：它们允许指标里写任意 SQL 吗？

### 3.1 开放度矩阵

（✓ 允许 ／ △ 受限或需绕道 ／ ✗ 不允许）

| 系统 | 单字段聚合 | 算术/函数表达式 | CASE WHEN | 条件聚合（指标内过滤） | 窗口函数 | 跨表 | 子查询 | 整段 SQL |
|---|---|---|---|---|---|---|---|---|
| **dbt / MetricFlow** | ✓ measure.agg | ✓ measure.expr（列级） | ✓【B】 | ✓ metric.filter（编译成条件聚合） | ✗（cumulative 由引擎生成） | ✓ 但是 **entity 声明**，不是 JOIN | ✗ | ✗（在 dbt model 层，语义层看不到） |
| **Cube** | ✓ | ✓ | ✓ | ✓ measure.filters | ✗（rollingWindow / cumulative 参数） | ✓ join + `{cube.measure}` | ✓ **cube 级 sql** | ✗ |
| **LookML** | ✓ | ✓ | ✓ | ✓ measure.filters | ✗（走 table calc） | ✓ explore join | △ derived table（表级） | ✓ **仅 derived table** |
| **Metabase** | ✓ | ✓ custom expression | ✓ case | ✓ CountIf / SumIf | ✗【C】 | ✓ 构建器 join | ✗ | ✓ 原生 SQL（但**不是 metric**） |
| **AtScale** | ✓ | ✓ MDX/DAX | ✓ IIF | ✓ 计算成员 + 集合过滤 | △ MDX 时间智能 | ✓ 模型内 join | ✗ | ✗ |
| **Malloy** | ✓ | ✓ | ✓ | △【C】 | ✓ 分析函数【C】 | ✓ join_one/many | ✓ 嵌套 query | ✓ SQL source |

### 3.2 三条硬结论

**结论 1：没有任何一家在「指标表达式」层放开任意 SQL。**
最强的三家（LookML / Cube / Malloy）开放的都是**表/来源层**：derived table、cube-level sql、SQL source。指标层永远是「列 + 行级表达式 + 封闭聚合枚举」。

**结论 2：真正的分界线是「聚合之外还能不能有查询结构」。**
- 子查询 = 引入新的 FROM → 破坏「指标只依赖已声明表」的不变式 → 全行业禁止。
- 窗口函数 = 引入「结果集上下文」→ 破坏「指标可独立下推/可缓存」→ 几乎全禁，改由引擎在 cumulative/rolling 场景内部生成。

**结论 3：每个系统都恰好保留了一个「逃生门」，且位置一致——在表层。**
这不是巧合：表层的 SQL 可以被**建模审查**（谁写的、什么时候改的、影响哪些指标），指标层的 SQL 不能。治理位置的选择就是产品设计。

### 3.3 为什么「单字段聚合」是共识起点

因为它是唯一**可交换、可下推、可合并**的形态：
- 可下推：`SUM(x)` 能推到任意粒度再上卷（additive）。
- 可缓存：同一 measure 在不同查询里生成同一段 SQL，预聚合才能命中。
- 可合并：多指标同查询时，WHERE/GROUP BY 可以共享。

一旦允许 `SUM(a)/SUM(b)` 直接进 measure，上面三条全部失效（除法不可上卷）。这就是为什么**比率必须升级为独立类型**，而不是「一个表达式」。

---

## §4 条件口径怎么表达：三种做法与优劣

### 4.1 三种做法

| 做法 | 代表 | 编译产物 | 语义 |
|---|---|---|---|
| **A. 指标自带 filters** | dbt metric.filter、Looker measure.filters、Cube measure.filters | `SUM(CASE WHEN cond THEN x END)`（条件聚合） | 「这个指标只在满足条件的行上算」 |
| **B. CASE WHEN 写进表达式** | Looker `type: number` + sql、Cube measure.sql、Malloy `case`、Metabase CountIf | 同上，但由人写 | 同 A，但无结构、无法被工具识别 |
| **C. segment / 维度切片** | Cube segments、Metabase segments、dbt saved query where、Looker always_filter | 查询级 `WHERE` | 「这次查询只看这些行」——**影响该查询里的所有指标** |

### 4.2 关键区别（很多人踩坑的地方）

在一个查询里同时要「整体」和「已支付」两个数时：
- 用 **A**：`revenue` 与 `paid_revenue` 同查询共存，各自条件聚合，**互不影响**。
- 用 **C**：加 `WHERE status='paid'` 后，`revenue` 也变成「已支付营收」，**你拿不到整体**。

所以 **A 与 C 不是同一种能力的两种写法，而是两种不同的语义**：
- A = **指标内条件**（度量定义的一部分，参与口径治理）
- C = **查询切片**（分析上下文，随时可变）

### 4.3 优劣势

| | 优点 | 缺点 |
|---|---|---|
| A（指标 filters） | 结构化、可治理、可被 LLM 工具识别、不影响其它指标 | 每加一个条件就多一个指标实体（指标爆炸）；条件只能引用已声明维度 |
| B（CASE WHEN） | 表达力最强、一次成型 | 不可解析 → 无血缘、无法下推、LLM 生成易错 |
| C（segment/WHERE） | 零成本、符合直觉 | 无法在同一查询内对比不同切片；容易与「指标口径」混淆 |

**行业收敛点**：**A 为主 + C 为辅**，B 只作为逃生门。dbt 和 Looker 都是这个组合（metric.filter + saved query where；measure.filters + always_filter）。

---

## §5 比率与占比

### 5.1 比率：为什么必须显式类型

四个真实口径：客单价（营收/订单数）、转化率（转化数/访问数）、复购率（复购客户/总客户）、退货率（退货单/总单）。

它们共享一个数学结构：**分子分母是两个独立聚合，最后才相除**。

- 正确：`SUM(revenue) / COUNT(DISTINCT order_id)`
- 错误（先除后加）：`AVG(revenue / order_qty)` —— 除非业务定义就是「行级比值的平均」

更隐蔽的错误是**粒度错配**：`SUM(a)/SUM(b)` 在 fan-out join（一对多）下，a 和 b 会被重复计数，比值可能仍然「看起来合理」但完全错。这就是 Looker 要发明 symmetric aggregates 的原因。

因此各家的做法：

| 系统 | 比率实现 | 是否显式类型 | 备注 |
|---|---|---|---|
| dbt | `type: ratio` + numerator/denominator | **是** | 分子分母可各自带 filter，可各自是不同 measure【B】 |
| Cube | calculated measure `{a} / NULLIF({b},0)` | 半显式（type: number） | 多阶段查询保证先聚合【B】 |
| LookML | `type: number` + `sql: ${a} / NULLIF(${b},0)` | 半显式 | 依赖 symmetric aggregates 正确性【A】 |
| Metabase | 无 | **否** | 只能原生 SQL 或可视化层 |
| AtScale | 计算成员（MDX 除法 + IIF） | 是（计算成员） | 引擎侧算 |
| Malloy | 直接写 `a / b` | 否（语言天然支持） | 编译器展开【B】 |

**共同工程细节**（值得抄）：
1. **除零必须显式处理**：`NULLIF(b, 0)`。返回 NULL 而不是 0（0 会污染平均值与图表）。
2. **分母必须独立聚合**，不能被分子的过滤条件污染。
3. **比率不可再加总**（non-additive）：上层若有 `SUM(ratio)` 必须报错或降级为「加权平均」。

### 5.2 占比（share of total）：为什么普遍不做进指标

占比 = 某维度成员的值 / 该维度全体的值。问题在于：**分母依赖「这次查询按什么分组」**。

- 按 `status` 分组 → 分母是全部 status 的合计
- 按 `region, status` 分组 → 分母是每个 region 内全部 status 的合计

同一个指标，两种分组，两个分母。因此占比**不是指标的属性，是结果集的属性**。三种实现：

| 实现 | 机制 | 谁在用 | 代价 |
|---|---|---|---|
| **窗口** `x / SUM(x) OVER (PARTITION BY ...)` | 在结果集上算 | 手写 SQL、Malloy（【C】）、部分语义层的多阶段查询 | 需要窗口支持；分区键必须由查询上下文给 |
| **总计聚合**（total / 二次查询） | 先查总计，再 join 回去 | Cube 的 total 参数【B】、BI 层 | 两次查询或一次多阶段 |
| **BI 层表计算** | 在结果表上算 | **Looker（percent_of_total）**、Metabase 图表、几乎所有 BI | 只在展示层存在，不能被 API 复用、不进血缘 |

**结论**：占比的正解是「**查询期能力**」而不是「指标定义」。谁把占比硬塞进指标定义，谁就会在换分组时算错。这也是为什么 dbt 与 Looker 都不提供原生 share-of-total 指标类型。

---

## §6 派生指标 / 依赖图 / 环检测

| 系统 | 派生形式 | 依赖是显式还是隐式 | 环检测时机 | 粒度 |
|---|---|---|---|---|
| **dbt / MetricFlow** | `type: derived`，expr 四则 + `metrics:` 输入清单 | **显式**（必须列输入） | 解析期（`dbt parse`）【B】 | 引用 **metric**（含其 filter/时间语义）【B】 |
| **Cube** | calculated measure（`sql` 里 `{measure}`） | 隐式（从 sql 里解析引用） | schema 校验 / 查询规划期【B】 | 引用 **measure**（cube 内的聚合，不含 metric 层概念）【B】 |
| **LookML** | `type: number` 的 sql 里 ${measure} | 隐式 | LookML 校验（IDE / 编译）【B】 | 引用 **measure**（含其 filters？——【C·未证实】，这是语义陷阱） |
| **AtScale** | 计算成员引用其它 measure | 隐式（MDX 成员引用） | 模型校验期【B】 | 引用 measure / 计算成员 |
| **Metabase** | 无 | — | — | — |
| **Malloy** | measure 表达式引用 measure | 隐式（表达式树） | 编译期（类型/作用域）【B】 | 引用 measure |

**三个值得抄的设计点**：

1. **dbt 要求显式列出输入 metrics**。看起来冗余（expr 里已经写了名字），但它让依赖图**不依赖解析表达式的成功**——图先成立，再校验表达式。这对「LLM 生成指标」特别重要：模型可以先声明依赖，再写表达式。
2. **依赖粒度决定语义**。引用「metric」意味着继承对方的 filter 与时间口径；引用「measure」意味着只继承聚合。**这是最容易出错的地方**（例：把「已支付营收」当作 measure 引用进比率，分母却用了含全部订单的 count）。业界没有统一答案，各家文档对此也不总是写清楚——**实现时必须自己定死并写进文档**。
3. **环检测必须在保存时，不在查询时**。查询时才发现环 = 用户看到 500。dbt 放在 parse 期，Cube 放在校验期。

---

## §7 语义层如何被 LLM/Agent 消费

### 7.1 三条技术路线

| 路线 | 机制 | 代表 | 风险 |
|---|---|---|---|
| **① 声明式清单进 prompt** | 把「指标名 + 口径描述 + 可用维度 + 可用聚合」渲染进 system | 我们仓库现状（metric_service.format_metrics_context） | 清单膨胀、截断后模型编 key |
| **② MCP / 工具调用** | 暴露 `list_metrics` / `get_dimensions` / `query_metrics` 等工具，模型按需取 | **dbt MCP server（2025）【B】、Cube MCP（2025）【B】、AtScale MCP【C】** | 工具契约设计；错误信息质量 |
| **③ 编译成 SQL 后给模型看/执行** | 语义层只当 SQL 生成器，模型自主执行 | 各种「Text-to-SQL + 语义层」混搭 | 口径漂移（模型改了 SQL 就脱离治理） |

### 7.2 各厂官方说法（区分宣传与事实）

- **dbt**：官方定位「Semantic Layer 是 AI 的可信数据接口」，并推出 **dbt MCP server**，让 agent 通过工具调用查询指标而不是写 SQL。【B·2025，具体工具名待核】
- **Cube**：官方定位「semantic layer = AI 的上下文层（context layer）」，强调「把指标定义喂给 LLM，而不是让 LLM 猜 schema」；有 MCP server 与 AI API。【B·2025】
- **AtScale**：官方定位「semantic layer for AI」，强调「模型看到的是业务语义不是表结构」。【C·未证实】
- **Looker**：Gemini in Looker / conversational analytics，走「自然语言 → LookML 查询」路线。【B】
- **Metabase**：Metabot，把 models/metrics 作为上下文。【B】

> **厂商宣传 vs 技术事实**：
> - 宣传：「有了语义层，AI 就能准确回答任何问题。」
> - 事实：语义层能保证的是「**模型只能在已定义的指标与维度空间里取值**」；它不能保证「用户问的问题在这个空间里有定义」。**覆盖率问题没有被解决，只是被暴露得更清楚**（模型会说「没有这个指标」而不是编一个）。

### 7.3 工程要点（从这三条路线里提炼）

1. **模型应该看到「能力清单」而不只是「指标清单」**：不仅要告诉它有哪些指标，还要告诉它「本系统支持什么形态」（单聚合？条件聚合？比率？），否则它会生成语法上合理、校验上必败的 formula。
2. **给模型一个自检工具**：`validate_formula` 比在 system prompt 里写十行规则有效。
3. **截断必须有兜底指令**：清单被截断时明确说「还有 N 个，请调用 list_metrics」——否则模型会编 key。（我们仓库已经这么做了，见 metric_service.py:273-278）
4. **返回编译后的 SQL 作为证据**：让模型/用户能看到「这句话被翻译成了什么」，是口径可信度的最后一道防线。

---

## §8 为什么它们都不敢放开任意 SQL

按「不放开的代价」排序，从硬到软：

### 8.1 正确性：fan-out 会静默算错（最硬的理由）

多表 join 后，一行订单会膨胀成多行明细。`SUM(amount)` 会重复计数。Looker 的 symmetric aggregates 就是为此发明的：把 measure 包进「先去重再聚合」的子查询。**一旦允许用户写任意聚合表达式，这个机制就无法保证正确**——因为引擎不知道你写的 `SUM(a*b)` 是否也该去重。

> 这是「表达力 vs 正确性」的正面冲突，也是所有 A 范式系统的第一约束。

### 8.2 方言与下推

语义层要一套定义跑 N 个引擎（Snowflake / BigQuery / Databricks / DuckDB / Postgres）。表达式层可以按方言翻译（函数映射表）；**整段 SQL 无法翻译**，只能原样下发——那就等于放弃多引擎能力。

### 8.3 缓存与预聚合

- Cube 的 pre-aggregations：靠「同一 measure 生成同一段 SQL」做匹配。
- AtScale 的 aggregate awareness：靠「完全理解查询结构」做改写。
- 任意 SQL → 每次查询形态都不同 → **缓存命中率归零**，预聚合全部失效。

### 8.4 权限与行级安全

行级权限（Looker access_filter、Cube 的 access policy、我们仓库的 schema 白名单）是在**已知表引用**的前提下注入谓词的。子查询/任意 SQL 可以：
- 通过 `FROM (SELECT ...)` 绕过表级检查
- 通过函数调用读文件（DuckDB 的 read_csv 就是现实例子）
- 通过 `UNION` 拼出未授权的表

这正是我们 sql_guard_ast.py 里 DANGEROUS_FUNCTIONS / check_table_refs 存在的原因——**护栏的存在本身就是「不能放开」的证据**。

### 8.5 血缘与影响分析

解析不了 = 不知道这个指标依赖哪些列 = 改表时无法评估影响 = 无法做「指标版本升级提示」。我们仓库已经有 MetricLineage 与 depends_on_metric_ids，一旦 formula 变成任意 SQL，这张图就废了。

### 8.6 治理与合同语义

指标是**组织内的合同**：同名指标必须同口径。允许写 SQL 的指标，本质是「每个人都能私自定义合同」，语义层退化成 SQL 编辑器。

### 8.7 小结：能力边界应该画在哪

| 层 | 允许什么 | 为什么 |
|---|---|---|
| **表/来源层** | 任意 SQL（子查询、CTE、窗口） | 可审查、可版本化、影响面可枚举 |
| **维度层** | 行级表达式（CASE WHEN、函数） | 不改变聚合语义，可下推 |
| **度量/指标层** | 封闭聚合枚举 + 四则 + 条件过滤 | 保证可合并、可缓存、可下推、可权限 |
| **查询层** | group_by / where / 时间粒度 / 排序 / limit | 上下文，随时可变 |

**这张表就是本文的最终答案**：不是「不能写 SQL」，而是「SQL 只能写在能被治理的那一层」。

---

## §9 对我们的可落地建议

> 现状锚点（已读代码确认）：formula 只允许 8 个聚合 + 四则 + `metric("key")`（metric_service.py:102-216）；派生解析用正则抽 `metric('...')`（metric_parser.py:15-32）；AST 护栏对窗口/UNION/SELECT */CROSS JOIN 有误拦（sql_guard_ast.py）；指标模型已有 version / formula_type / depends_on_metric_ids / MetricLineage（models/metric.py:11-68）。

| 优先级 | 建议 | 改哪里 | 加什么能力 | 为什么（对应本文） |
|---|---|---|---|---|
| **P0** | **把 formula 校验从「正则 + 手写字符扫描」换成「sqlglot 表达式 AST + 节点白名单」** | `backend/app/services/metric_service.py:105-216`（`_validate_derived_expression` / `assert_formula_allowed`） | 白名单节点：AggFunc / Column / Literal / Paren / BinaryOp / **Case**；先只放开 **CASE WHEN**（条件聚合），仍禁子查询与窗口 | §3、§4：条件口径（退货率、支付成功率）目前**无法表达**，只能靠查询级 WHERE，导致同查询内无法对比整体与切片 |
| **P0** | **新增结构化字段，而不是让用户拼四则**：`filter`(JSON: field/op/value[])、`time_grain`、`numerator_formula` / `denominator_formula` | `backend/app/models/metric.py:46-65`（加列 + 迁移）、`metric_service.resolve_to_expression:340-368` | formula_type 扩为 basic / derived / **ratio**；ratio 生成 `NULLIF(den,0)` 且分子分母**各自独立聚合** | §5.1：`SUM(a)/SUM(b)` 在 group_by 下粒度错配；显式 ratio 是行业共识（dbt/Looker/Cube 三家一致） |
| **P1** | **占比不要做成指标，做成查询期开关** | `metric_service.resolve_metric:371-402`（ChartQueryConfig 加 `share_of_total: bool`）、query_engine 渲染侧 | 生成 `x / SUM(x) OVER (PARTITION BY <group keys>)` 或两段查询；**不让用户写窗口** | §5.2：占比分母依赖分组，是结果集属性；写进 formula 必然在换分组时算错 |
| **P1** | **依赖图从正则升级为 AST 抽取，并在保存时校验** | `metric_parser.parse_derived_formula:15-32`、`metric_service` 保存流程（depends_on_metric_ids 写入处） | AST 抽引用（不再匹配字符串）；保存时校验：被引用指标存在、可见、无环；父指标 version 变更时给子指标打 stale 标记 | §6：dbt 的做法证明「依赖先成立、再校验表达式」更稳；我们已有 detect_cycle 但入口是正则 |
| **P2** | **护栏按「语义类别」分层，而不是一张黑名单** | `sql_guard_ast.py`（check_aggregations:266-300 / check_union:168 / check_select_star:219 / check_cross_join:195） | 窗口函数走独立分支（`exp.Window` 单独判定，不进 AggFunc 白名单）；UNION ALL 且两侧投影同源时放行；`COUNT(*)` 与 EXISTS 里的 `*` 不算 SELECT *；CROSS JOIN 只在显式写法 + 无谓词时拦 | §8.4：护栏误拦的根因是「用一个维度的规则去判所有语义类别」，与本文「能力边界要分层」同源 |
| **P2** | **给 LLM 的指标上下文补「能力边界声明」+ `validate_formula` 工具** | `metric_service.format_metrics_context:281-350`、`backend/app/services/agent_tools.py`（工具注册） | 上下文里写明「本系统支持：单聚合 / 四则派生 / CASE WHEN；不支持：窗口、子查询」；工具返回具体拒绝原因（第几个字符、哪个节点非法） | §7.3：模型需要的不是更多示例，而是**可判定的边界**；错误信息质量决定重试收敛速度 |
| **P3** | **指标 DSL 文档化：把「引用粒度」写死** | `docs/`（新增指标 DSL 规范页）+ `metric_service` docstring | 明确 `metric("key")` 引用的是「含 filter 的完整口径」还是「纯聚合」；ratio 的分母是否继承分子 filter | §6：这是全行业最模糊的一处，不写死必然在比率指标上出错 |

**如果只能做两条**：P0 的两条。它们把「条件口径」和「比率」从「不可表达」变成「可表达且可治理」，是我们与 Metabase 式「保存聚合」拉开差距的最小投资。

---

## 附录 A：待复核清单（C 级结论，按风险排序）

| # | 待核结论 | 复核方式 | 影响的建议 |
|---|---|---|---|
| 1 | dbt derived 的 `metrics:` 输入清单是否为当前必需语法（vs 早期 Jinja `{{ Metric() }}`） | 查目标 dbt 版本的 derived 文档 + dbt-semantic-interfaces JSON schema | 若已改，§2.1 示例需重写 |
| 2 | LookML `type: number` 引用某 measure 时，**是否继承该 measure 的 filters** | 官方 measure types + filters 文档，或本地编译一段 LookML | 影响「引用粒度」的设计（P3） |
| 3 | Cube 跨 cube 计算度量的确切语法与 join 要求 | Cube 文档 calculated-members | 仅影响参考，不影响我们实现 |
| 4 | AtScale 计算成员语言（MDX / DAX）与 SML 版本 | AtScale 官方文档 | 影响 §2.5 的可信度 |
| 5 | transform.co 是否被 Google 收购、「Transform AI」是否同一实体 | 官方公告 / 工商信息 | 影响选型结论（当前建议：不列入候选） |
| 6 | Metabase metric 是否已支持 metric 引用 metric（v50+） | Metabase changelog / docs | 影响 §2.4 的「下界样本」论断 |
| 7 | Malloy 的 SQL source 语法与分析函数清单 | malloydata 文档 | 影响 §2.6 |

## 附录 B：本文用到的官方入口（全部未逐字复核，见文首声明）

- dbt：<https://docs.getdbt.com/docs/build/semantic-models> ／ <https://docs.getdbt.com/docs/build/metrics-overview> ／ <https://docs.getdbt.com/reference/metric-properties>
- Cube：<https://cube.dev/docs/product/data-modeling/concepts/measures> ／ <https://cube.dev/docs/product/data-modeling/concepts/calculated-members>
- Looker：<https://cloud.google.com/looker/docs/reference/param-measure-types> ／ <https://cloud.google.com/looker/docs/symmetric-aggregates> ／ <https://cloud.google.com/looker/docs/derived-tables>
- Metabase：<https://www.metabase.com/docs/latest/data-modeling/metrics> ／ <https://www.metabase.com/docs/latest/data-modeling/segments>
- AtScale：<https://www.atscale.com/>
- Malloy：<https://www.malloydata.dev/> ／ <https://github.com/malloydata/malloy>
