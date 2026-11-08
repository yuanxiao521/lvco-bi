# 09 · 产品标杆：AI 与「指标中心/语义层」的结合形态，以及「AI 自动创建指标」到底做到哪一步

> 调研时间：2026-11（各页面的官方 Last updated 已在正文标注）
> 证据等级：✅ 官方文档原文（本机已抓取正文）｜⚠️ 厂商宣称（营销页/新闻稿）｜❔ 未证实（无公开一手细节，仅有二手转述或产品页标题）
> 对照基准：我们仓库现状（8 聚合 + 四则 + `metric("key")`；raw SQL 走 sqlglot AST 白名单；存在窗口函数/UNION/SELECT * 误拦、CROSS JOIN 拦错方向）

---

## 0. 结论先行

1. **"AI 生成语义模型/指标定义"已经是成熟产品能力，但没有任何一家把"AI 直接发布指标"作为默认路径。** Databricks、Snowflake、Power BI、Zenlytic、FineBI 五个互不相关的产品，全部收敛到同一形态：**AI 产出草稿 → 结构化差异/建议 → 人点"接受" → 才落库生效**。这不是产品保守，而是由"指标出错的责任结构"决定的（§4.3）。
2. **"AI 写指标"的落点已经分岔成三类**：写语义层 YAML/SQL 表达式（Databricks、Snowflake、Cube）、写 DAX（Power BI）、写"Agent 私有度量定义"（Databricks Genie knowledge store、Zenlytic dynamic field）。**没有一家让 LLM 直接产出裸 SQL 并绕过语义层**——Snowflake Cortex Analyst 只出 SQL，且必须 grounding 到 semantic view。
3. **试算（Preview）已是准入门槛，不是加分项**：Databricks 每个 field/measure 都有 Preview 按钮；Snowflake 生成后进 Semantic Studio 复核；Power BI 先在 DAX query view 里跑通再"加入模型"。
4. **治理度量没有行业标准公式**，目前全是各厂自造口径（verified queries、覆盖率、复用率）。这对我们是机会：可以用工程手段把口径定义清楚，做成可展示的产品能力。
5. **中小 BI 的空白不在"生成"，而在"AI 起草的指标如何与既有指标库去重/合并/淘汰"，以及"变更前的影响面预演"**——大厂靠客户侧的数据治理团队兜底，中小客户没有这个人。

---

## 1. 证据与方法说明（先声明可信边界）

- 本机网络经本地代理，抓取不稳定。**下文所有 ✅ 结论均来自我实际抓取到正文的页面**；只拿到 URL + 标题、未取到正文的产品（如 ThoughtSpot 的公式能力细节、Fabi 的语义层细节）一律标 ❔，不做推断式补全。
- 二手媒体（SiliconANGLE、TechTarget、厂商公众号）只在无法找到一手文档时用于"存在性"佐证，并标 ⚠️。
- 全文不引用任何未经我核实的百分比。凡是数字，都能在文中找到对应链接与上下文。

---

## 2. 产品对照表

图例：**能否直接写指标** —— 指"AI 能否产出/修改一条可被下游复用的指标定义"，而不是"AI 能否临时算一个数"。

| 产品 | AI 与指标/语义层的关系 | AI 能否直接写指标定义 | 草稿态 | 试算/预览 | 人工审批 | 版本与回滚 | 证据 |
|---|---|---|---|---|---|---|---|
| **ThoughtSpot**（Spotter / SpotterModel / Spotter Semantics） | 语义模型（TML，Analytics-as-Code）+ Agent 消费；SpotterModel 专门做"自动化语义建模" | **能（模型层）**，但定位是"人验证过的定义" | 有（TML 文件即代码，走 Git 流程） | 有（查询即验证） | **有，且是核心卖点**："Human-Verified Definitions" | 有（官方称支持 version control 与 roll back） | ✅ [Spotter Semantics 博客](https://www.thoughtspot.com/blog/spotter-semantics)、[BI Agents 产品页](https://www.thoughtspot.com/product/agents) |
| **Databricks**（Genie + UC Metric Views） | Metric View 是 UC 一等对象（YAML/SQL DDL），Genie Agent 消费；**Genie 会在度量编辑器里"建议度量"** | **能**：Genie Code 用自然语言生成/导入 metric view；Genie suggestions 一键生成度量 | 有（编辑器内建议 → 点击创建） | **有**：每个 field/measure 都有 Preview | 有（建议需点击；编辑需 CAN EDIT 权限） | 对象级（UC 治理、协作编辑、血缘） | ✅ [Metric views 总览](https://docs.databricks.com/aws/en/uc-semantics/metric-views/)、[Create a metric view](https://docs.databricks.com/aws/en/uc-semantics/metric-views/create)（Last updated 2026-10-12） |
| **Snowflake**（Cortex Analyst / Semantic Views） | 语义视图 = schema 级对象（YAML/DDL），Cortex Analyst 只在其上生成 SQL | **能**：Semantic View Autopilot + CoCo 对话式生成"逻辑表/关系/指标" | **有**：YAML 文件 + CoCo 的 inline diff | 有（创建后 Semantic Studio / YAML 复核；verified queries 校验） | **有**：diff 逐条 accept/reject；权限门槛 CREATE SEMANTIC VIEW | 有（schema 对象 + 权限/共享体系；YAML 可导出） | ✅ [YAML 规范](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)、[Semantic View Autopilot](https://docs.snowflake.com/en/user-guide/views-semantic/autopilot) |
| **Power BI Copilot** | Copilot 在 DAX query view 里用自然语言写 DAX；度量是"先写成查询里的 measure，再手动加入模型" | **能写 DAX**（=指标表达），但**不直接改模型** | **有**（DAX query view 就是草稿区） | 有（DAX 查询可直接执行看结果） | **有**：必须人工执行"加入模型" | 弱：模型层无指标级版本（靠部署管线/工作区） | ✅ [Quick measure suggestions with Copilot](https://learn.microsoft.com/en-us/power-bi/transform-model/quick-measure-suggestions)（Last updated 2025-03-21） |
| **Cube** | 语义层（SQL-first YAML）+ MCP/Agent 消费；**Semantic Model Agent 可从数仓表生成模型、转换 LookML、用 YAML 扩展** | **能（模型层，生成 YAML）** | 有（YAML 文件，天然走 Git） | 有（模型可编译校验、查询验证） | 有（人审 YAML 并提交） | **有**：模型在 Git 里 | ✅ [BI for Agents](https://cube.dev/agentic-ai) |
| **Zenlytic** | Clarity Engine：语义层不完整时**动态生成度量/维度**回答当前问题 | **能（临时）**：dynamic measures/dimensions；**promote 后才进语义层** | 有（dynamic field 即草稿） | 有（结果附带可解释说明） | **有（promote 动作）**：数据团队审查后提升 | 语义层侧有（模型代码） | ✅ [The Clarity Engine](https://zenlytic.com/blog/clarity-engine) |
| **Hex Magic** | Notebook Agent：生成/修改 SQL、Python、图表；不做指标定义管理 | **否**（改的是查询与 notebook 单元格） | 有（写入单元格，人工执行） | 有（跑单元格） | 有（人执行/提交 notebook） | 有（Git 集成） | ✅ [Hex Magic 产品页](https://hex.tech/product/magic-ai/) |
| **Fabi.ai** | AI 分析师 + notebook + MCP server + GitHub 版本控制 | ❔ 未见指标定义自动生成的一手文档 | — | — | — | 有（GitHub 版控） | ✅ [fabi.ai](https://www.fabi.ai/)（仅确认能力面，不确认指标起草） |
| **帆软 FineBI 7.0 指标中心** | 指标中心 = 模型管理 + 指标/维度管理 + 指标集 + 数据目录；**为 FineChatBI 提供语义层支撑** | ❔ 公开文档只讲"零代码/公式定义 + 拖拽 + 数据预览"，未见 AI 起草指标 | **有**（多人协作"仅经确认的版本生效"） | 有（"数据预览便于校验正确性"） | **有**（"所有变更需手动提交才生效"；数据目录有申请审批） | **有**（版本控制 + 血缘 + 发布/申请审批） | ✅ [指标中心简介](https://help.fanruan.com/finebi/edition-view-27804-37.html) |
| **阿里云 Quick BI（智能小Q）** | 小Q问数/解读/搭建三个 Agent，基于数据集与语义模型问答；**增值模块，按席位售卖** | ❔ 未见"AI 生成指标定义"的一手文档；能力集中在问数/解读/搭建 | — | 有（问数结果可预览/下钻） | 有（对话式搭建，人确认成稿） | ❔ | ✅ [智能小Q概述](https://www.alibabacloud.com/help/zh/quick-bi/user-guide/smartq) |
| **观远数据（观远 Metrics + 问数 Agent）** | 明确把指标中心定位为"AI 分析的指标语义底座"，支持财务口径/业务口径并存、责任人、版本变化 | ❔ 未见 AI 起草指标的一手文档 | ❔ | ❔ | ❔ | 有（口径/责任人/版本变化） | ✅ [观远指标中心升级](https://www.guandata.com/blogdetail/zhi-biao-zhong-xin)（2026-06-17） |
| **数势科技（SwiftMetrics + Data Agent）** | SwiftMetrics 定位"指标语义本体"，口号是"让数据具备被 AI 调用的语义能力" | ❔ | ❔ | ❔ | ❔ | ❔ | ✅ [digitforce.com](https://www.digitforce.com/) |
| **Kyligence（Zen + Copilot）** | Copilot 基于指标平台做问数/归因/预警；**指标仍由人用图形化方式定义，AI 负责"把自然语言匹配到指标语义"** | **否**（2023 公开材料中明确是"匹配"而非"创建"） | — | 有（对话即查询） | — | ❔ | ✅ [Kyligence Copilot 发布（2023-07-19）](https://cn.kyligence.io/blog/ai-kyligence-copilot-2/) |

**读表要点**：上表里"能写指标"的 6 家（ThoughtSpot / Databricks / Snowflake / Power BI / Cube / Zenlytic），**没有一家把写入做成默认自动的**；"不能写"的 4 家（Hex / Fabi / QuickBI / Kyligence）则把能力集中在"问数/分析"，指标定义留给人工或客户既有治理流程。

---

## 3. 逐个拆解（只写一手证据支撑的部分）

### 3.1 ThoughtSpot：把"人验证过的定义"做成了产品概念

- 官方把语义层定义为 Agent 的"罗塞塔石碑"：_编码业务定义、安全规则、join 逻辑与计算语义，让 Agent 以概率方式理解意图、返回确定性结果_。✅ [博客](https://www.thoughtspot.com/blog/spotter-semantics)
- **三大支柱**（原文）：① **Human-Verified Definitions**——"tribal knowledge is defined and verified by humans, providing human-in-the-loop validation"，且宣称"token-based approach"让验证**不需要看 SQL**，从而可以众包定义、而不是一次性建模；② Built for Agents；③ Deterministic Query Generation。
- 语义模型格式是 **TML（ThoughtSpot Modeling Language）**，官方叙事是 **Analytics-as-Code（AaC）**：版本控制、自动化测试、CI/CD、**roll back**。✅ 同上
- ThoughtSpot 是 **Open Semantic Interchange（OSI）** 的创始成员之一，主张语义定义标准化以跨工具互通。✅ 同上（该标准后续演进见 §3.14 Apache Ossie）
- 产品线把"建模"单独拆成一个 Agent：**SpotterModel**，面向数据工程师做"Automated Semantic Modeling"。⚠️ [产品页](https://www.thoughtspot.com/product/agents)；页面上"200x Spotter adoption in a year"属营销数字，不可当采纳率引用。
- ❔ **未证实**：Spotter / SpotterModel 是否支持"用自然语言生成 formula（计算字段表达式）"的具体交互与限制。公开一手文档中我未取到该细节，不做断言。

### 3.2 Databricks：Metric View 是"有权限、能预览、可被 Agent 建议"的一等对象

- 定位（原文）：_Metric views are the core implementation of Unity Catalog semantics... define metrics once and query them at runtime_；与传统视图的区别是"定义一次 sum(revenue)/distinct customers，用户可 group by 任意字段，查询引擎生成正确计算"。✅ [Metric views](https://docs.databricks.com/aws/en/uc-semantics/metric-views/)（2026-10-29）
- **三条创建路径**：① Catalog Explorer 可视化编辑器（Builder 下拉拼表达式 / Custom 手写）；② YAML 编辑器；③ **Genie Code：用自然语言描述你要什么**。✅ [Create a metric view](https://docs.databricks.com/aws/en/uc-semantics/metric-views/create)（2026-10-12）
- **AI 建议度量**（原文）：_"When you add a measure, Genie suggestions appear at the top of the measure editor pane; click a suggestion to create that measure."_ ✅ 同上 —— 这是"AI 起草 + 一次点击接受"的最小形态，值得直接抄交互。
- **试算**（原文）：每个 field / measure 定义完有 **Preview** 按钮，"run the transformation and view the result"。✅ 同上
- **面向 Agent 的元数据**：metric view 支持 synonyms、display name、format、governed tags，官方明确目的是"improve agent accuracy"；还有 **Relationships between metric views (Beta)**，让 AI 助手和 BI 工具选对 join 路径。✅ [Metric views 总览](https://docs.databricks.com/aws/en/uc-semantics/metric-views/)
- **Genie 家族**已拆成 Genie One（业务用户）、Genie Agents（数据团队配置可信数据/指标/业务规则）、Genie Code（开发者）。✅ [Genie](https://docs.databricks.com/aws/en/genie/)（2026-10-27）
- **Genie Agent 侧的"指标"是另一种落点**：knowledge store 里可以放 **SQL expressions（measures / filters / dimensions）**，每个 agent 上限：Instructions 100 条、knowledge store snippets 200 条；并可加 example SQL、UC functions、verified answers。✅ [Tune Genie Agent quality](https://docs.databricks.com/aws/en/genie-agents/tune-quality)
- 一句官方提醒值得抄进我们的设计文档：_Genie operates in a nondeterministic manner, it's important to make the guidance free from conflicting or ambiguous information_——即**语义层的价值一半在"消除冲突"，不只在"提供定义"**。✅ 同上

### 3.3 Snowflake Cortex Analyst：把"AI 起草"做成了带 diff 的向导

- **语义视图（Semantic View）是 schema 级对象**，接 RBAC / 共享 / 元数据目录；旧版"存 stage 的 semantic model YAML"仍兼容但官方推荐新对象。✅ [YAML 规范](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec)
- YAML 结构（原文，可直接当我们的 schema 参考）：`tables[].base_table{database,schema,table}` 或 `definition: <SQL query>`；`primary_key` / `unique_keys`；`dimensions[]{name,synonyms,description,expr,data_type,is_enum,...}`；measures 同理；还有 relationships、filters、verified queries、custom instructions。✅ 同上
- **Semantic View Autopilot（AI 起草）** 的关键设计，逐条抄作业：✅ [Autopilot](https://docs.snowflake.com/en/user-guide/views-semantic/autopilot)
  - 输入四类上下文：自然语言描述、示例 SQL（问题+SQL 的 CSV）、**Tableau/Power BI 文件反导入**、已有 YAML。
  - 生成器对输入的处理是**"提取 → 复核 → 再写入"**：示例 SQL 先"validate、throw out invalid"；"extract all tables and columns and **present them for review before adding**"；从查询历史里挖关系与 verified query 建议。
  - **AI 生成表/列描述是 opt-in 勾选项**（"select whether you want to add AI-generated descriptions"）——把"AI 写文档"降级成一个可选开关，风险外置。
  - 工程约束写成产品建议：第一次做语义视图**建议不超过 10 张表、50 列**（非硬限制）——和我们 `format_metrics_context` 的 30 条/1200 字上限是同一类思路。
  - **CoCo 对话式**：生成 YAML 后，"**each change appears as an inline diff that you can accept or reject**"；示例对话里用户说"Add a profit margin metric"、"把 ORDERS 与 RETURNS 的 join 改成 return_order_id"，AI 出 diff。
  - 生成后 **Cortex Analyst 会自动产出改进建议**，人来 "review them and apply as needed"。
- **提准确率的三件套**（官方）：verified（"gold"）queries、review & accept **suggested** verified queries（基于真实使用）、custom instructions 引导 SQL 生成。✅ [Improving accuracy with AI](https://docs.snowflake.com/en/user-guide/views-semantic/improving-accuracy)
- 结论：**Snowflake 是"AI 起草 + diff 审批"完成度最高的公开样板**——它的每一步都能映射到我们要做的东西（描述→提取候选→复核字段→生成→diff→接受→verified queries 反哺）。

### 3.4 Power BI Copilot：会写 DAX，但"加入模型"必须人点

- 原文：_Quick measure suggestions to assist creation of DAX formulas for model measures using natural language is available in **DAX query view** using Microsoft Fabric Copilot_；_"You can create measures in a DAX query that can be **added to your model when you're ready**"_。✅ [Quick measure suggestions](https://learn.microsoft.com/en-us/power-bi/transform-model/quick-measure-suggestions)（2025-03-21）
- 也就是说：**草稿区是 DAX query view，审批动作是"加入模型"**。另有"Quick measures（模板化）"与"visual calculations（可视化内计算）"作为不需要 Copilot 的替代路径。✅ 同上
- 版本从 2024-10 起才有该 Suggestions 能力（该版本之前显示为 disabled）。✅ 同上
- ❔ 未证实：Copilot 能否**修改既有度量**（而非新建）、是否有审批流/审计字段。Power BI 的版本治理在"工作区/部署管线/OneLake"层面，**不是指标级**——这一点对我们反而是可打的差异点。

### 3.5 Cube：把"生成模型"写成独立 Agent，且天然 Git 化

- 原文（Data Modeling 区）：_"**Bootstrap with the Semantic Model Agent** — Generate a model from your warehouse tables, convert existing LookML, or extend definitions in SQL-first YAML"_；"Every agent reasons over governed definitions instead of raw tables"。✅ [cube.dev/agentic-ai](https://cube.dev/agentic-ai)
- 交付形态是 **YAML 文件**（在客户自己的仓库里）→ 版本、评审、回滚全部由 Git 承担，产品不需要自建审批流。这是一个**低成本治理**范式，值得我们对照取舍（我们是 SaaS 内置指标库，没有 Git，所以必须自建 draft/version）。
- MCP / API / CLI 三种接入，让 Claude、Codex、ChatGPT、Cursor 等外部 Agent 复用同一份语义模型与权限。✅ 同上

### 3.6 Zenlytic：唯一把"临时指标"放开的厂商，用 promote 兜底

- Clarity Engine：先让 Zoë 写强 SQL，再"把查询映射回语义层里的结构化定义"；**语义层不完整时，动态创建度量/维度**来回答当前问题。✅ [Clarity Engine](https://zenlytic.com/blog/clarity-engine)
- 治理兜底（原文）：_"Data teams can review all queries, **promote helpful dynamic fields to the semantic layer**, and maintain full control over analytics logic"_；每个动态答案都有 in-GUI 解释。✅ 同上
- 这是"**允许 AI 自由造，但只有人能转正**"的路线：和我们"AI 起草 → 人审批 → 走 create API"在哲学上完全一致，区别只是它把"造"发生在查询时（runtime），我们发生在建指标时（authoring time）。
- ⚠️ 页面上的"Reduce quick data pulls by 57%"是营销数字，非采纳率。

### 3.7 Hex Magic：AI 写的是"分析代码"，不是"指标"

- Magic 的定位是 notebook 内的 agent：生成/编辑 SQL 与 Python、scaffold 应用、debug 查询。✅ [Hex Magic](https://hex.tech/product/magic-ai/)
- 人类在环的方式很物理：**AI 写进单元格，人执行**。✅ 同上
- 对我们的启示：**"AI 产出的东西落在哪个容器里"决定治理成本**。Hex 选了"单元格"所以不需要指标审批；我们选了"指标中心"就必然需要审批链。

### 3.8 Fabi.ai
- 确认存在的能力面：AI 分析师/agent、AI Python notebook、**MCP server**、GitHub 版本控制、workflow 定时投递。✅ [fabi.ai](https://www.fabi.ai/)
- ❔ **未证实**：是否有语义层/指标定义，以及 AI 能否起草指标。文档未见一手说明，不建议在对外材料中引用。

### 3.9 帆软 FineBI 7.0 指标中心（国内最接近"指标中心"形态的产品）
- 定位（原文）：_"提供统一数据模型、指标加工、指标管理与指标应用的全链路解决方案，实现数据「可管理、可置信、可消费」的闭环，**为 FineChatBI 提供强大的语义层支撑**_"。✅ [指标中心简介](https://help.fanruan.com/finebi/edition-view-27804-37.html)
- **治理机制写得比国外产品更细**（可直接对标我们的缺口）：
  - 指标/维度管理 = "跨表计算、动态维度和零代码指标配置 + 元数据标签化治理 + **资产全生命周期运营（含发布、版本控制与申请审批）**"；
  - **多人协作**："仅经确认的版本生效"、"所有变更需**手动提交**才生效，杜绝未完成修改影响生产环境"；
  - **数据预览**："数据预览便于校验正确性"（= 试算）；
  - **同义冲突**被明确列为要解决的问题："统一指标口径，避免「同名不同义/同义不同名」"；
  - 血缘双向穿透（前溯数据集、后追看板/报表/派生指标）。
- ❔ 未证实：7.0 是否有"AI 起草指标"。官方该页只讲零代码/公式/拖拽，AI 部分落在 FineChatBI（问数）侧。

### 3.10 阿里云 Quick BI 智能小Q
- 三个 Agent：小Q问数、小Q解读、小Q搭建；**增值模块、需额外购买、按席位分配**，仅高级版/专业版可增购，且分站点可用。✅ [智能小Q概述](https://www.alibabacloud.com/help/zh/quick-bi/user-guide/smartq)
- 能力集中在"问数/解读/搭报表"，页面自述优势是"100 万+专项训练语料、20+种调优手段"（⚠️ 厂商口径）。✅ 同上
- ❔ 未证实：小Q 是否能生成/修改**指标定义**（而非计算字段或图表）。公开文档未涉及。

### 3.11 观远数据
- 产品矩阵：观远 BI / **观远 Metrics（企业统一指标语义平台）** / 问数 Agent / 洞察 Agent / DecideX。✅ [指标中心升级](https://www.guandata.com/blogdetail/zhi-biao-zhong-xin)（2026-06-17）
- 叙事值得抄（QA 体）："BI 解决看见数据，指标中心解决**数据按什么口径来算**"；"先从 5-10 个核心经营指标切入，边用边沉淀"；"支持财务口径/业务口径并存"；"指标中心可以作为 AI 分析的**指标语义底座**，让 AI 基于**已确认的指标定义**去问数、分析和归因"。✅ 同上
- ❔ 未证实：是否支持 AI 起草指标。

### 3.12 数势科技
- 定位："以「**指标语义本体 + AI Agent**」为核心技术"；SwiftMetrics = "指标治理底座，统一企业数据语言，**让数据具备被 AI 理解、调用和治理的语义能力**"。✅ [digitforce.com](https://www.digitforce.com/)
- 这家的营销话术几乎就是我们想讲的故事（AI-Ready 语义层），可作为话术基准，但**无公开技术细节**（如何校验、如何审批）❔。

### 3.13 Kyligence
- 2023 年 Copilot（预览版，基于 Azure OpenAI GPT-3.5）：自然语言问指标、归因、预警、嵌入飞书；**指标由用户通过图形交互定义，Copilot 负责"把自然语言提问匹配到指标里的语义信息"**。✅ [发布博客](https://cn.kyligence.io/blog/ai-kyligence-copilot-2/)
- 这条证据很有价值：**国内最早一批"AI + 指标平台"产品，起点是"消费指标"而不是"生产指标"**——说明"AI 写指标"是更晚、更难的一步，不是自然延伸。

### 3.14 行业级变量：语义模型标准正在统一（Apache Ossie / 原 OSI）
- OSI（Open Semantic Interchange）已进入 Apache 基金会，更名 **Apache Ossie (incubating)**：目标是"vendor-agnostic semantic model specification"，**一套 JSON/YAML 规范，任何工具可读写**；仓库里有 core-spec、ontology、Python SDK（Pydantic v2）、compliance（portability grader + query test cases），以及 **dbt / GoodData / Polaris / Salesforce 的参考转换器**。✅ [apache/ossie README](https://github.com/apache/ossie)
- 它明确要解决的三个问题，正好是我们文档第 4 节要讨论的："同一个 KPI 在不同工具里定义不同"、"团队花大量时间手工对账"、"AI Agent 基于不一致的业务逻辑产出不可靠结果"。✅ 同上
- 对我们的含义：**指标定义的可移植性会变成采购问题**。中小 BI 若能"导入/导出 Ossie"，等于免费获得与 dbt/大厂生态互通的名分（成本不高，见 §9 建议 7）。

---

## 4. 核心问题：有没有产品让 AI 直接写/改指标定义？

### 4.1 先把"直接写"拆成 4 档（这是本文最有用的分析框架）

| 档位 | 定义 | 典型实现 | 谁做到了 |
|---|---|---|---|
| **M0 只读消费** | AI 只能引用已存在指标 | Kyligence Copilot、QuickBI 小Q、观远问数 | 国内主流 |
| **M1 内联建议** | AI 在编辑器里给出候选度量，点一下就创建 | **Databricks Genie suggestions** | Databricks |
| **M2 草稿 + diff 审批** | AI 产出完整定义草案，逐条 accept/reject 后落库 | **Snowflake Autopilot / CoCo**、**Power BI DAX query view → 加入模型**、**Cube Semantic Model Agent + Git 评审** | Snowflake、Power BI、Cube |
| **M3 运行时自由造 + 事后转正** | 查询时动态生成临时指标，数据团队 promote 才进语义层 | **Zenlytic dynamic fields → promote** | Zenlytic |
| **M4 全自动发布** | AI 写完直接生效，无人工闸门 | **无人实现（截至 2026-11，未见任何公开产品）** | 0 家 |

> 注：ThoughtSpot 的 SpotterModel 我没有取到"生成后如何落库"的一手细节，按 M2 归类（TML 走版本控制 + human-verified 定义），标 ❔ 待补。

### 4.2 结论
**有，但都停在 M1–M3。** 具体证据：
- Databricks：Genie Code 自然语言生成 metric view + Genie suggestions 一键建度量 ✅
- Snowflake：Autopilot 生成逻辑表/关系/指标；CoCo 出 inline diff ✅
- Power BI：Copilot 写 DAX，人再"加入模型" ✅
- Cube：Semantic Model Agent 从数仓表生成模型 ✅
- Zenlytic：dynamic measure + promote ✅
- FineBI：**具备完整的"草稿→提交→审批→版本"机制，但 AI 起草环节未见公开**（❔）

### 4.3 为什么没人放开到 M4（四个结构性原因，按硬度排序）

1. **口径污染不可逆**（技术）：一个指标被 N 个看板/订阅/告警/API 引用，改错一次，下游全部静默偏移，且**没有断言能自动判定"毛利率口径对不对"**。Databricks 官方那句"nondeterministic…avoid conflicting or ambiguous information"就是承认了这点。✅
2. **责任归属必须可落到人**（组织）：出了问题要回答"谁改的、依据什么、谁批的"。所以 Snowflake 把权限（CREATE SEMANTIC VIEW）与 diff accept 都留在人身上，FineBI 直接写死"所有变更需手动提交才生效"。✅
3. **评测不可行**（工程）：LLM 生成的 formula 语法正确 ≠ 语义正确。所以厂商全部把验证外包给"试算 + verified queries 对照 + 人的领域知识"，而不是自动评测。✅
4. **商业责任**（商业）：厂商不敢替客户的口径签字。于是"AI 起草 + 人审批"变成**责任转移装置**，而不只是安全措施。

> 反向证据（说明 M4 并非不可想象）：Zenlytic 在 **runtime** 放开了"自由造指标"，代价是这些指标**不进语义层、不被复用、每次都要重新生成**。这恰好证明：**放开的边界 = 复用范围**。一旦要复用，就必须有人签字。

---

## 5. "AI 起草 + 人审批"在业界叫什么？有公开数据吗？

### 5.1 术语（按可检索性排序）
| 说法 | 出处/语境 | 备注 |
|---|---|---|
| **Human-in-the-loop (HITL) validation** | ThoughtSpot 原文："human-in-the-loop validation" ✅ | 最通用的学术/产品词 |
| **Human-Verified Definitions** | ThoughtSpot 三大支柱之一 ✅ | 最像"产品名"的说法，可直接借 |
| **Verified ("gold") Queries** | Snowflake Cortex Analyst ✅ | 不是审批术语，但是"人签过字的查询"，同一治理思想 |
| **inline diff / accept or reject** | Snowflake CoCo ✅ | 交互层术语，最适合写进 PRD |
| **suggestions → apply** | Snowflake Autopilot（"review them and apply them as needed"）✅ | |
| **promote to the semantic layer** | Zenlytic ✅ | "转正"动作最准确的动词 |
| **added to your model when you're ready** | Power BI ✅ | 最谦逊的表述 |
| **AI-assisted authoring / assistive** | Databricks："Databricks AI assistive features" ✅ | 定位词：assistive ≠ autonomous |
| **Analytics-as-Code (AaC)** | ThoughtSpot ✅ | 治理范式，不是审批词 |

> 建议我们在产品里用 **"AI 起草 / 人工审定（Human-Verified）"** 这一组词：既对齐国际术语（便于对外讲），又比"AI 生成指标"准确。

### 5.2 公开数据：基本没有，不要引用别人的数字
- 我**没有找到任何**厂商公开"AI 起草指标的采纳率 / 审批通过率 / 节省工时"的一手数据。搜索到的都是营销页数字：ThoughtSpot "200x Spotter adoption in a year"⚠️、Zenlytic "Reduce quick data pulls by 57%"⚠️、QuickBI "100 万+专项训练语料"⚠️。**这些都不是"AI 起草指标"的采纳率**。
- 唯一有工程含义的公开量化是**约束条件**而非效果：Snowflake 建议首个语义视图 **<10 张表、≤50 列**；Databricks Genie Agent 上限 **Instructions 100 条 / knowledge store 200 条**。✅ 这两条可以直接拿来当我们 prompt 与上下文的预算依据。
- 结论：**采纳率必须自己测**。我们已有 eval 基线（docs/research/05 提到 300 指标=6348 token 的实测），可以顺势建立"AI 起草指标三率"：草案产出率、校验通过率、人工采纳率。这是简历上比"支持 AI 建指标"硬得多的数字。

---

## 6. 指标覆盖率/治理度量的叙事：业界怎么度量"语义层建设成果"

### 6.1 厂商实际在讲什么（营销话术层）
| 叙事 | 谁在讲 | 原话/要点 |
|---|---|---|
| 口径统一度 | FineBI | "避免「同名不同义/同义不同名」" ✅ |
| 资产复用 | FineBI / 观远 | "指标可跨报表、跨应用、跨业务复用"；"口径变更时应用层自动同步更新" ✅ |
| 血缘可追溯 | FineBI | 双向穿透（前溯数据集、后追看板/派生指标）✅ |
| 责任到人 | 观远 | "统一管理指标名称、计算口径、适用场景、**责任人**和版本变化" ✅ |
| AI 可信度 | 观远 / 数势 | "让 AI 基于**已确认的指标定义**去问数"；"让数据具备被 AI 调用的语义能力" ✅ |

### 6.2 可计算的度量口径（我给的工程定义，业界无统一标准）
> 明确声明：**以下公式不是行业标准**，是我基于上文产品机制提出的可落地定义；业界目前没有公开的统一口径（❔ 就"标准公式"而言）。

| 指标 | 定义 | 取数口径 | 现状 |
|---|---|---|---|
| **指标引用率（覆盖问题占比）** | 引用命名指标的查询数 / 总查询数 | 每次执行查询时打点 | 我们**已有**：`get_semantic_coverage()`（进程内计数器） |
| **指标复用率** | 被 ≥2 个资产（看板/画布/报表/API）引用的指标数 / 活跃指标总数 | metric_usage 聚合 | 表已存在，缺聚合端点 |
| **口径冲突数** | 同 name 但 formula 不同的指标对数；或同 formula 但 name 不同的对数 | 全表自连接 | 未做（FineBI 明确把这个当卖点） |
| **血缘完整率** | 有 depends_on / lineage 记录的派生指标占比 | metric_lineage | 表已存在 |
| **草案采纳率** | 被审批通过的 AI 草案数 / AI 草案总数 | 新增字段 | 未做（这是差异化指标） |
| **变更影响面** | 单次口径变更影响的资产数（看板/报表/订阅） | /impact 端点 | 我们**已有**：`GET /metrics/{id}/impact` |

### 6.3 营销话术怎么写（可直接用）
> "指标语义层覆盖率从 0 → xx%，指标类问题不再由 LLM 自由生成 SQL；AI 起草的指标草案人工采纳率 xx%，口径冲突数从 xx 降到 0。"
> —— 三个数都必须是**我们自己打点算出来的**，每个都要能指到一个 API / 一张表。这比"支持 AI 自动建指标"有说服力得多。

---

## 7. 差异化机会：中小 BI 在"指标语义层 + AI"上的空白

按"大厂做不了/不愿做 & 我们能做"排序：

1. **去重与合并（dedupe/merge），而不是生成。** 大厂默认客户有治理团队来做"同名不同义"的合并（FineBI 把它写成功能点，但仍需人）。中小客户没有这个人。**AI 起草时第一等公民应该是"建议复用/合并"，而不是"建议新建"**——这是我们可以做成"默认行为"的地方。
2. **变更前的影响面预演。** 大厂靠 CI/CD + 组织流程；我们有 `/metrics/{id}/impact` 与 lineage 表，可以做到"审批页上直接显示：这次改口径会影响 3 个看板、1 个订阅"——审批人 3 秒就能决策。这是**审批体验**的差异，而不是 AI 能力的差异。
3. **口径冲突检测（conflict lint）。** 静态检查"同义不同名/同名不同义"，在 AI 起草的入口就拦掉。没有厂商把它做成自动 lint（FineBI 只是把它列为目标）。
4. **低成本试算。** 大厂要求语义视图 <10 表/50 列；我们可以做"小样本 + 短时间窗 + 与既有指标对比"的三重试算，把风险压到最低，代价是几秒钟的查询。
5. **轻量审批（1 人审）。** 大厂的做法是工作流引擎（申请审批、发布审批、目录审批三层）。中小企业只要"起草人 ≠ 审批人"这一条硬约束 + 审计留痕。**做减法**是差异点。
6. **中文业务语义的自动补全。** Snowflake 的 synonyms 要手写，Databricks 的 synonyms 要手填。中文场景里"销售额/营收/收入/GMV/成交额"的同义词与口语表达，是 AI 最擅长、也最能体现效果的地方——**而且这是我们模型天然的优势**。
7. **可移植出口（Apache Ossie）。** 现在做映射成本低，将来是采购加分项（尤其面向有 dbt/大厂数仓的客户）。

---

## 8. "AI 起草指标"的最佳实践清单

### 8.1 交互步骤（9 步，按 Snowflake Autopilot + Databricks Genie 的公开设计整理）

1. **入口即上下文**：用户用一句话描述"要什么口径"，系统同时注入**已有指标清单**（我们有界注入：30 条/1200 字）。
2. **先检索再生成**：先在指标库里做相似度检索，返回"可能已存在"的 3 条候选，让用户选"复用/基于它改/新建"。→ 把去重前置。
3. **AI 产出结构化草案**（不是 SQL 自由文本）：`{key,name,description,formula,agg_kind,formula_type,depends_on,datasource_id,table_ref}`。**永远不要让模型直接产出裸 SQL 落库**。
4. **静态校验（三道）**：结构白名单 → 字段存在性 → 依赖成环/key 冲突。校验失败时回传"具体哪一段不合法"，让模型自纠一轮（最多 2 轮）。
5. **试算**：小样本（limit ≤ 50）+ 短时间窗 + 与"用户口述的目标值/既有相近指标"对比，展示样本行、空值率、量级差异。
6. **diff 呈现**：如果是修改既有指标，逐行 diff（改了什么、影响哪些下游资产）；新建则展示"与最相近指标的差异"。
7. **人工审定**：审批人看到"草案 + 校验报告 + 试算结果 + 影响面"四件套，一键通过/驳回/要求修改。
8. **落库即版本**：走既有 create API；版本号 +1，change_note 记录 `source=ai_draft`、模型名、prompt_hash、草案 id。
9. **反哺**：审批通过的草案与被驳回的理由入库，作为下一次的 few-shot 与"负样本黑名单"。

### 8.2 护栏项（checklist）

| # | 护栏 | 为什么（证据） | 我们怎么落 |
|---|---|---|---|
| 1 | **禁止 AI 直出可执行 SQL** | Cortex Analyst 只出 SQL 且必须 grounding；M4 无人实现 | prompt 契约限定 JSON schema |
| 2 | **公式结构白名单** | 我们已有 `assert_formula_allowed` | 复用，且**必须放宽**（见 §9 的 P0-3） |
| 3 | **字段存在性校验**（防幻觉列名） | Genie 靠 sample queries/knowledge store 消歧；Snowflake 让用户勾选列 | 与 datasource 元数据比对，硬失败 |
| 4 | **依赖成环检测** | 派生指标可引用指标 | `metric_parser.detect_cycle` 已有 |
| 5 | **key 唯一性预检** | DB 有 `uq_metrics_key_user`，但报错不友好 | 起草阶段就查，返回"已存在，建议复用" |
| 6 | **试算必跑** | Databricks 每个度量都有 Preview | 不跑通不允许提交审批 |
| 7 | **审批人 ≠ 起草人** | FineBI "仅经确认的版本生效" | 最小 RBAC 硬约束 |
| 8 | **变更必留痕** | ThoughtSpot AaC / FineBI 版本控制 | metric_versions + change_note |
| 9 | **上下文有界** | Snowflake <10 表/≤50 列；我们 30 条/1200 字 | 保持现有上限，勿放大 |
| 10 | **合成指标（模板 `{{field}}`）不允许 AI 起草** | `resolve_to_expression` 按 dimensions 顺序替换占位符，语义脆弱 | 起草阶段直接拒绝 formula_type=template |

### 8.3 反模式（明确不要做）
- ❌ 让 AI"顺手把指标也改了"（隐式写操作）——M4 无人做，且会毁掉审计。
- ❌ 用"AI 生成的指标数量"当 KPI——会诱导模型批量造垃圾指标，直接推高口径冲突数。
- ❌ 审批页只放 formula 文本——审批人看不懂 SQL 就会盲批。ThoughtSpot 的"token-based 验证不需要看 SQL"就是在解决这个。
- ❌ 把试算结果当"正确性证明"——试算只能证明"能跑、量级不离谱"，不能证明口径对。

---

## 9. 对我们仓库的可落地建议（≤7 条，按优先级）

> 前提：我们的方案是「指标中心加 AI 起草 → 三道校验 → 试算 → 人审批 → 走现有 create API」。下面每条都指到文件/能力，不新增架构。

**P0-1. 新增起草服务与端点，先不落库。**
新建 `backend/app/services/metric_ai_service.py`：`draft_metric(db, user_id, nl_text, datasource_id, hints)` → 返回候选 JSON + 校验报告（不写库）；在 `backend/app/api/v1/metrics.py` 加 `POST /metrics/draft`（`require_editor`，与现有 POST 同权限）。同时给 `backend/app/services/agent_tools.py` 加一个 `create_metric_draft` 工具——目前该文件只有 `list_metrics`（L1374），AI 侧完全没有"起草"能力，这是最大的功能空洞。

**P0-2. 把三道校验抽成独立模块，复用现有实现。**
新建 `backend/app/services/metric_validator.py`：① 结构 → 复用 `assert_formula_allowed`（metric_service.py L193）；② 字段存在性 → 与 datasource 列元数据比对；③ 依赖 → 复用 `parse_derived_formula` + `detect_cycle`（metric_parser.py L15/L49），加 key 唯一性预检（对应 `uq_metrics_key_user`）。**不重写校验逻辑**，只做编排与错误信息归一化。

**P0-3. 修公式校验的过严问题（否则 AI 起草会被大面积误拦）。**
`_validate_derived_expression`（L105）是手写状态机，不支持负数、CASE、嵌套聚合，而 AI 起草派生指标必然产出这些形态。建议改为用 **sqlglot 解析表达式**（仓库已依赖 sqlglot），在白名单函数集合 + `metric("key")` 引用规则上做 AST 判断。这与 raw SQL 护栏（sql_guard_ast.py）方向一致，可共用函数白名单（`ALLOWED_AGGREGATIONS`，L34）。

**P1-4. 加试算端点，复用现有查询链路。**
`POST /metrics/{id}/preview` 或 `POST /metrics/draft/preview`：`resolve_metric`（metric_service.py L371）→ `execute_chart_query`（query_engine.py L607），**limit ≤ 50、超时 5s、只跑一个维度**，返回样本行 + 空值率 + 与最相近指标的数值差异。审批页必须带上这个结果（对应护栏 #6）。

**P1-5. 草稿态与审批：用最小改动复用现有表。**
`metric.py` 加 `status`（draft/pending_review/active/rejected，默认 active 保持兼容）；审批走 `POST /metrics/{id}/approve`，权限 `require_editor` 且强制"审批人≠起草人"；`metric_versions.change_note` 统一写 `source=ai_draft;model=...;prompt_hash=...`。回滚直接复用 `rollback_to`（L912）与 `POST /metrics/{id}/rollback`——**这部分我们比 Power BI 和 Quick BI 都强，别浪费**。

**P2-6. 治理度量从"进程内计数器"升级为可展示资产。**
`get_semantic_coverage()`（L42）现在是内存字典，重启即清零。改为落库（或 Prometheus gauge），并补两个口径：**指标复用率**（metric_usage 聚合）与**口径冲突数**（同 name 不同 formula 自连接）。再加 **AI 草案采纳率**。这四个数就是简历与产品页上的四行字（§6.3）。

**P2-7. 预留 Apache Ossie 导出/导入（低成本期权）。**
先只做"指标定义 → Ossie JSON"的单向映射（key/name/description/formula → measure 的 expr + 语义字段），不做全量兼容。价值在于：一旦客户问"能不能和 dbt/大厂语义层互通"，我们有答案；且强迫我们把 formula 的表达收敛到**可移植子集**（这本身就能减少口径歧义）。

---

## 附：本文未证实清单（不要引用）

- ❔ ThoughtSpot Spotter/SpotterModel 是否支持自然语言生成 formula（计算字段）
- ❔ ThoughtSpot 生成后的落库/审批具体交互（按 M2 归类是推断）
- ❔ Power BI Copilot 能否修改既有度量、是否有度量级审计
- ❔ Fabi.ai 是否有语义层与 AI 起草指标
- ❔ Quick BI 小Q 是否能生成/修改指标定义
- ❔ 观远、数势是否有 AI 起草指标
- ❔ FineBI 7.0 是否有 AI 起草指标（治理机制完整，但 AI 环节未见一手文档）
- ❔ 业界是否有"AI 起草指标采纳率"的公开统计（我未找到）
