# 02 · 数据分析 Agent 编排链路对标与「对话 / 画布」解耦方案

> 调研员：arch-research ｜ 任务：task-2 ｜ 日期：2026-11-07
> 范围：编排范式对比 · 正确性保障环节差距 · Lead 循环五维优化 · 对话/画布解耦 3 方案 · 双助手差异配置表
>
> **标注约定**：**[事实]** = 可在本仓库代码或一手文档直接核对；**[推断]** = 基于事实的判断，需评测验证；**[二手]** = 第三方转述/未读到一手文档，可信度降级。
> 本文所有"我们"均指 `E:\BI\LvcoBI\lvco-bi` 仓库当前 HEAD。

---

## 0. 一页结论

1. **编排范式没有"更先进"，只有"是否匹配任务形状"。数据类 Agent 的生产形态正在从"一次性 Text2SQL"收敛为「确定性管线 + 局部 agentic 决策」**：Wren AI 把"正确性"拆成 7 个支柱（schema linking / value profiling / ambiguity detection / generation trace / retry & repair / eval + 可观测）和一组可编排原语（dry-plan / dry-run / structured error / memory / skills），Vanna 2.0 从"RAG 生成 SQL"演进为"agent + tool + 流式 UI 组件"，Dify 则在固定 workflow 里嵌一个"Agent Node"承担局部自治。我们现在的「**单循环 Lead（决策/评审）+ 派发到 planner-executor 内核（执行）**」正是这两层的组合，方向正确，**不需要推倒重来**。[事实 + 推断]

2. **我们的真实短板不在"循环怎么转"，而在"正确性原语"不全**：语义层只有"指标清单"缺"生成前校验（dry-plan）"；schema 只有"全量 dump"缺"检索/裁剪"；自修复只有"prompt 劝告 + error hint"缺"错误码 → 策略表"。这是与 Wren AI 差距最大的一环，也是"看起来能用、偶尔编数字"的根因。[事实 + 推断]

3. **我们比较强的两处（比同类开源项目平均做得多的部分）**：① SQL 安全 4 层防护（L1-L3 关键字/意图 + L4 AST 白名单）；② **数字后验** `verify_report_numbers` + **两阶段评审**（HARD 代码硬闸 / SOFT 裁量）。这两项应作为"教学可解释性"卖点保留，不要在重构中丢掉。

4. **单循环五维最大收益点（按性价比排序）**：
   - (a) 派发后 `refresh_canvas` 把新布局写回 **system 中段**，导致整段前缀 KV-cache 失效（与 Manus "append-only / 前缀稳定"原则冲突）→ 改 `context/provider.py:refresh_canvas` + `context/assembler.py:for_agent_loop`；
   - (b) 观察类工具在循环里**串行 await**，多观察一次就多一整轮 LLM → 改 `lead_agent_unified.py` 工具循环为 observe 并发；
   - (c) 工具结果截断**有两处**（`payload[:4000]` 与 800 token 预算），违反本仓库 `compress.py` 模块头自述的"截断只允许出现在这里"铁律 → 删除前者。

5. **对话 / 画布解耦：推荐 A（共享 Lead 循环 + capability profile）**，加权 4.25/5（B = 3.25，C = 1.5）。但 **A 必须包含"把 prompt 拆成公共底座 + 入口 profile"这一步**——现状 `LEAD_AGENT_SYSTEM` 里约 1/3 篇幅是画布专属规则（KPI 卡片、清空画布、落块顺序），说明"一份 prompt 服务两个入口"**已经在泄漏**，只加 profile 而不拆 prompt，等于把泄漏制度化。

6. **中期（6-12 个月）目标态是 B**：两个 Lead profile 各自演化 prompt 与工具集，**共享执行层 / 工具层 / 事件协议**。C（完全独立）只有在第三入口的产品形态与现有两端都差异极大（如"报表生成器""移动端问答"各自独立团队）时才值得，否则是三倍运维成本换零收益。

7. **两助手的定位差异必须落成配置表，不能落成 if**。`lead_worker.py` 的 `ENTRY_WORKERS` 已经证明"表驱动"在本仓库有效（新增入口 = 加一行），同一模式应扩展到「工具集 / 上下文源 / 终止条件 / 产物 / 重试策略」——本文 §6 给了可直接抄的表。

8. **成本最可能失控的位置：Lead 循环没有"每请求 LLM 调用预算"**。子内核有 `_MAX_LLM_CALLS_PER_TASK=24`，Lead 循环只有步数上限 6（每步 1 次）+ 派发内部 N 次 + 评审 0~1 次，**未设总预算、也未记账**。[事实]

9. **两个最不确定的判断（详见 §8）**：① 单循环 vs legacy 的真实时延/成本差缺线上对拍数据（代码注释里的 +32% 是 20 题小样本）；② 我们是 DuckDB **单表宽表**模型（`information_schema ... table_name='data'`），抄 Wren AI 的 schema linking / join path 收益可能很小——需要"字段基数"数据才能定投入。

10. **一句话给决策者**：把预算投到"正确性原语 + 上下文预算从记账变强制"这两件事上，收益远大于"换编排框架（LangGraph 等）"；后者在本文调研的所有一手材料里，都只是**实现手段**，不是能力来源。

---

## 1. 现状速写（先定事实基线，后面所有对标都基于此）

| 层 | 事实 | 落点 |
|---|---|---|
| 入口路由 | `ENTRY_WORKERS`：canvas→{complex: canvas, simple: react}；chat→{complex: orchestrator, simple: react}；未登记入口兜底 orchestrator | `agents/lead/lead_worker.py:35-69` [事实] |
| Lead 主循环 | 单 system + 工具循环 + 代码层收敛闸；步数上限 `LEAD_AGENT_MAX_STEPS=6`；配额 observe 2 / dispatch 3 / repair 2 | `lead_agent_unified.py:138,255`、`lead_agent_tools.py:49`、`config.py:235` [事实] |
| 循环开关 | **默认 `LEAD_AGENT_MODE="legacy"`**，单循环需显式置 `single_agent`；出问题可一键回滚 | `config.py:233`、`lead_agent.py:404,495-498` [事实] |
| 工具面 | 10 个：观察 5（get_canvas_layout / list_datasources / list_fields / list_metrics / query_sample）+ 执行 2（dispatch_analysis / repair）+ 终止 3（answer / ask_user / stop）；**写工具永不注册** | `lead_agent_tools.py:25-49,136` [事实] |
| 上下文装配 | 唯一门面 `TurnContextProvider.for_agent_loop`，每步重建完整 messages；状态源恒定 + 工具结果块增长；tool_result 逐条按 `TOOL_RESULT_MAX_TOKENS=800` 压缩 | `context/provider.py:115-144`、`context/assembler.py:266-350`、`config.py:259` [事实] |
| 预算表 | `equivalent_agent_loop_table`：system 1200 / tool_schema 1947 / tool_result 800 / memory 1333 / window 2000，**`enforced=False`（只记账不裁剪）** | `context/budgets.py:116-137` [事实] |
| 评审 | 派发后**固定工序**（非可选工具）：HARD 代码闸（执行失败/零落块/工具链失败）→ 仅 SOFT 非空时一次 LLM 裁量；HARD 非空强制 fail 不得翻案 | `lead_review_stage.py:90-149` [事实] |
| 数字后验 | 报告里的数字 token 与查询结果数值集比对（容忍 ×100 / ÷1e4 / ÷1e8 单位换算） | `lead_tools.py:475-545,964` [事实] |
| SQL 安全 | L1 输入注入检测 / L2 意图分析 / L3 关键字黑名单 + 自动 LIMIT / L4 AST（仅 SELECT、聚合白名单、禁 CROSS JOIN、禁 SELECT *、schema.data 表引用格式） | `sql_guard.py:1-8`、`sql_guard_ast.py:1-15` [事实] |
| 数据模型 | 每个数据源在 DuckDB 里是 **单 schema 单表 `data`**（宽表），查询引擎从 `information_schema.columns ... table_name='data'` 取列 | `query_engine.py:50-65` [事实] |
| 缓存 | ① 派发幂等键 memo（含 guidance 摘要）；② 查询结果缓存 `query:{ds}:{user}:{hash}` | `lead_tools.py:213`、`query_engine.py:700-710,846-848` [事实] |
| 事件流 | Redis Stream 可回放事件日志（XADD/XREAD，2000 条上限，1h TTL），断线续传按事件 ID | `event_stream.py` [事实] |
| 执行内核 | react（ReAct 图，MAX_ITERATIONS 6 / 连败 5 熔断 / 查询 5 次上限）、orchestrator（Planner→Executor→Report，单步重试 3，LLM 预算 24）、canvas（报告骨架→落块，单步工具 4 / 超时 45s / 同签名 3 次跳过） | `react_agent.py`、`agent_orchestrator.py:41-48`、`canvas_orchestrator.py` [事实] |
| 图引擎 | 自研 `Graph`（Node/Edge/条件路由/共享 State/steps_log），**零依赖，非 LangGraph 包** | `agents/graph.py:1-30` [事实] |

> 小结（**推断**）：我们不是"没有编排"，而是"编排分了三层（Lead 循环 / worker 派发表 / 执行内核图），每层各有一套收敛闸与重试策略，**但三套闸之间没有统一预算与统一错误码**"。这解释了为什么"单点都对、整体偶发不收敛"。

---

## 2. 链路范式对比（必答 1）

### 2.1 五种范式的定位

| 范式 | 代表实现（一手） | 在数据分析场景的优势 | 典型失败模式 | 适用规模 | 我们的位置 |
|---|---|---|---|---|---|
| **单循环（ReAct / tool loop）** | Anthropic《Building effective agents》定义的 agent；Claude Agent SDK 的 `gather context → take action → verify work → repeat` | 灵活、上下文统一（"看→判→决"同一份认知）、新增能力只需加工具 | ①无代码闸就空转/成本失控；②长任务 context rot（模型注意力预算被耗尽）；③行为不可复现，回归测试难写 | ≤ 6~10 步、产物数量少、语义判断为主 | **Lead 层就是单循环**（`stream_unified`），且已配了步数闸/配额闸/终止动作三道代码闸——这是同类里做得规范的 |
| **planner-executor（两段式）** | Dataherald 的两个 LangChain agent（RAG-only 与 fine-tuned LLM-as-a-tool）；我们的 `agent_orchestrator`（Planner→Executor→Report） | 计划可见可审、步骤可并行、失败可按步重试；"规划"与"填参数"解耦，各自 prompt 更短 | ①计划漂移（计划基于旧快照，执行时环境已变）；②步骤间隐性依赖丢失（Planner 不知道前面实际产出了什么）；③规划幻觉（计划里出现不存在的工具/字段） | 5~30 步、多产物、结构相对稳定 | **派发之后就是它**；已用 Pydantic schema 校验 + 校验失败回喂重试 1 次（`planner_agent.py:348-370`） |
| **graph 状态机** | LangGraph（checkpointer/thread_id + store + interrupt + time travel）；Dify Workflow 的 Agent Node；DB-GPT AWEL（operator→AgentFream→DSL 三层 DAG） | 可中断/可恢复/可回放/HITL 天然；状态迁移显式，审计友好；多入口共享同一张图 | ①图膨胀（节点数爆炸后没人敢改）；②节点间状态契约脆弱（少一个 key 就静默降级）；③"改流程要改代码"，非工程师无法参与 | 生产级长流程、需要"断点续跑"、合规审计 | 自研 `Graph` 只覆盖**内核内部**（react/canvas/orchestrator），**Lead 层没有图化**；断点续跑能力等价物是「Redis Stream 事件回放 + memo 幂等键」，缺"从中断步恢复执行" |
| **固定 workflow 管线** | Vanna（v0.x 主流形态：检索上下文 → 生成 SQL → 执行 → 错误回喂）；我们的 `query_engine`（ChartQueryConfig → SQL → DuckDB）。注：Wren AI 处在"管线"与"单循环"之间——流程由 primitives 固定，但**何时调用哪个 primitive 交给 agent** | 确定性高、可评测（golden set）、单次成本可预测、正确性可分层加固 | ①覆盖不了开放问题（"帮我看看有什么异常"）；②需要前置投入（语义层/MDL/训练集），冷启动重 | 高频同类问题（"销售额趋势""Top N"） | 我们的 `query_engine`（参数化 ChartQueryConfig → SQL → DuckDB）是这一档，但**没有 dry-plan 层**（生成前不校验展开后的 SQL） |
| **沙箱代码执行** | Claude Agent SDK"给 Agent 一台电脑"（bash + 文件系统）；Code Interpreter 类（E2B/Firecracker 类隔离）；Julius AI（[二手] 以沙箱内 Python 跑分析） | 图灵完备：任意清洗/建模/可视化；一次写码多次复用；能处理"非结构化脏数据" | ①安全隔离成本高；②依赖地狱与不可复现；③延迟高（装包/启动）；④**数字正确性仍需外部校验**（代码跑通 ≠ 口径对） | 重度分析、数据预处理、探索性建模 | **未上沙箱**；我们用 DuckDB 内嵌 + 参数化查询覆盖核心分析。**判断：现阶段不需要沙箱**（见 §4.2 工具维度） |

### 2.2 三条跨范式的观察（对决策最重要）

1. **"workflow vs agent"的分界线是「可验证性」**。Anthropic 原文的立场是"能用最简单的方案就别上 agent"；Wren AI 更进一步：把每一步中**能确定性校验的部分固化成 primitive**（dry-plan 看展开 SQL、dry-run 连库校验、structured error 给修复线索），只把"选哪个 primitive、何时放弃"留给模型。[事实：两篇一手文档] **我们现在恰好相反的兴趣分布**：循环与评审写得比多数项目细，但"生成前校验"这一层是空的。

2. **沙箱不是编排范式，是"能力上限的开关"**。Claude Agent SDK 的关键设计不是"用了什么图"，而是"给了 Agent 一台电脑"，于是工具集退化成 bash+文件，能力上限由"会不会写代码"决定。它的代价是**上下文管理与安全性全部转移到你身上**（所以 Manus 才会写一整篇 context engineering）。[事实] **推断**：对 BI 场景，沙箱的边际收益主要在"数据清洗/特征工程"，而这正好是我们想给**对话助手**的定位——这是一个真实的、值得评估的缺口，但优先级低于正确性原语。

3. **Graph 状态机的价值不在"好看"，在 checkpoint 与 HITL**。LangGraph 的持久化页明确把 checkpointer 的用途列为"会话连续性、human-in-the-loop、time travel、故障容错"；DB-GPT 的 AWEL 也是为了让"围绕数据的编程更确定"。[事实] 我们现在的 HITL 是 `confirm_guard` 三态机（短路、不调 LLM），比"图里加一个 interrupt 节点"更省 token，但**只在 Lead 入口有**，子内核内部没有中断点。

---

## 3. 数据类 Agent 的「正确性保障」环节清单（必答 2）

> 顺序按"生成前 → 执行中 → 生成后"排列。**"我们有没有"以代码事实为准；"缺哪一环"必须给可落点，否则不算结论。**

| # | 环节 | 别人怎么做（一手来源） | 我们有没有 | 差距判断 | 具体落点 |
|---|---|---|---|---|---|
| 1 | **语义层（指标口径）** | Wren AI：MDL（models/relationships/views/cubes）+ `knowledge/rules` + 编译产物 `target/mdl.json`；planning engine 用 sqlglot + CTE 改写把"建模 SQL"展开为"可执行 SQL"。Vanna：把 DDL/文档/历史 SQL 对做向量检索（RAG），"训练"= 往向量库灌上下文。Dataherald：golden Q↔SQL 对做 few-shot + fine-tune。 | **有半边**：`metric_service`（key/name/formula/description）+ 指标血缘 `metric_lineage`（source_field/transform/target_field）+ `format_metrics_context` 全量清单注入 Lead（入口处 4000 字符上限）+ `list_metrics` 工具 + `resolve_to_expression` 把 formula 解析成 SQL 聚合表达式。 | **缺"生成前校验"**：没有 dry-plan（不执行、只展示展开后的 SQL 供人/模型核对），也没有"这个指标用在这些维度上是否合法"的校验。指标是**全量注入**而非按问题检索，指标多了会挤爆上下文也会稀释注意力。 | ①新增 `dry_plan(goal/metric_key/dims)` 观side-effect-free 工具 → `query_engine` 复用现有 SQL 构造但只返回 SQL 文本；②`format_metrics_context` 增加 top-k 裁剪（按 key/别名与用户消息的相关度，先做字符/关键词级，不上 embedding）；③把 `metric_lineage` 接进评审：报告里出现某指标时，可追溯到源字段，作为"数字可信"的证据链。 |
| 2 | **schema 检索 / 裁剪** | Wren AI 列为正确性支柱之一「Schema linking：知道哪些 models/columns/relationships 与问题相关」，靠 MDL + memory retrieval（`wren memory fetch`）做**检索**而不是全量喂。Vanna 同类：向量检索相关 DDL。 | **只有"全量 dump + 按需拉全表"**：上下文里每个数据源只有 `id/name/type`（`sources/state.py:20-27`）；要字段就调 `list_fields`，返回**该数据源全部列 + 样本值 + 敏感列**（`agent_tools.py:438-512`），无排序无裁剪。 | 中等（**对我们可能不大**）：我们每个数据源是 DuckDB 单表 `data` 宽表，**没有 join 路径问题**，所以 Wren 的 schema linking 收益主要落在"字段很多时选对列"。若单表字段 > 100，全量 dump 会吃掉 2-4k token/次。 | ①`ListFieldsTool` 增加可选 `keywords` 参数，按列名/样本值做粗筛后返回（默认行为不变）；②长期：在 `DatasourceSource` 里注入"列清单压缩版"（列名 + 类型，无样本），把样本值降级为按需。**注意**：这一步必须配评测，否则容易改坏"猜不到列名"。 |
| 3 | **SQL guard（安全）** | 各家都有；Wren 的取向是"agent 不应拥有对仓库任意对象的无限自由"（PR #2311 标题即此主张）[二手，来自搜索结果的标题]。 | **强**：L1-L3 正则/意图 + L4 AST（仅 SELECT/WITH、子查询深度、禁 UNION/CROSS JOIN/SELECT *、聚合白名单、禁危险函数、表引用必须 `schema.data` 格式、自动 LIMIT）。 | 差距小。**唯一隐患**：L1/L2 是正则 + 中文关键词，会误伤（用户说"删除这列"被拦）也漏报（换语言/拆字）。 | 保留；建议把 L1/L2 的命中从"拦"改为"记事件 + 交给 confirm 流程"，减少误伤（`sql_guard.py` 调用点）。 |
| 4 | **SQL 自修复** | Wren：`wren dry-run`（连库校验不取数）+ **structured errors** + "agent retry workflows"，把"错在哪、怎么改"结构化成可编排的一步。Dataherald：SQL 执行工具把 DB 报错回喂给模型自纠错。Vanna 同理。 | **有但不结构化**：靠 prompt 强制（"错误信息中的 table_ref 和可用列名是唯一可信来源，直接复制重试"）+ 代码熔断（react `MAX_CONSECUTIVE_FAILURES=5`、planner 重试 1 次、orchestrator 单步 ≤3 次、canvas 同签名 3 次跳过）。 | **缺"错误码 → 策略"的表**。现在是"模型看 hint 自己决定"，同一类错误在不同内核里策略不一致（react 5 次 vs canvas 3 次 vs orchestrator 3 次）。 | ①在 `tool_executor` / `query_engine` 的 error 分支统一产出 `{code, hint, retryable}`（建议码表：TABLE_NOT_FOUND / COL_NOT_FOUND / TYPE_MISMATCH / EMPTY_RESULT / TIMEOUT / PERMISSION）；②新增 `repair_policy.py`：`code → action(retry_once | refetch_schema | downgrade_to_sample | give_up)`，由代码执行而不是 prompt 劝告；③三个内核统一引用同一张表。 |
| 5 | **采样校验** | 通用做法：先取小样本确认数据形态（类型、枚举值、空值），再跑全量。Wren 称之为「Value profiling：知道 `status=4` 到底是什么」。 | **有**：`query_sample`（5 行，`lead_observe.py:88`）；`add_chart_block` 后端自验证取数（画布编排器专门移除了裸查数工具就是因为它）；`_validate_measure_types` 在拼 SQL 前拦"对 VARCHAR 做 SUM"（`query_engine.py:68`）；`ListFieldsTool` 带 3 行样本值。 | **缺"结果 sanity"**：没有"结果为空 / 全 NULL / 只有 1 行 / 行数比预期暴增"这类**结果级**检查。这类问题现在只能靠 SOFT 裁量（LLM 看摘要）发现，成本高且不稳定。 | 在 `assess_subtask_detail`（`lead_tools.py`）增加 HARD 项：`rows == 0`、`所有数值列为 NULL`、`single_row_agg_returned_instead_of_series`。这三条**纯代码可判**，符合"HARD 不需要 LLM"的既有设计。 |
| 6 | **结果对账** | Wren：generation trace（"答案是怎么构造的：models/joins/CTEs/展开 SQL"）+ eval（golden NL-SQL，schema/prompt 变更时跑回归）。Dataherald 明确说"评估 AI 生成的 SQL 是否准确极其困难"，所以才要 golden set。 | **有数字后验（少见）**：`verify_report_numbers` 把报告里的数字与"查询结果数值集"比对，容忍 ×100 / ÷1e4 / ÷1e8 单位换算；两阶段评审 HARD/SOFT。 | **两个缺口**：①**自证循环**——claims 来自工具结果，报告数字又与 claims 比，等于"自己验自己"，若工具取数本身错（口径错/过滤错）验不出来；②**没有跨子任务对账**（同一指标两次派发得到不同值，现在无人发现）。 | ①跨子任务对账：在 `ctx.turn_summaries` 里保留每次派发的 `{metric_key → value}`，同 key 不同值时记 SOFT "口径不一致"（落在 `lead_observe` 或 `lead_review`）；②把"展开 SQL"写进 `RunAnalysisResult`（现在有 tool_chain，可扩展存 plan SQL），让评审能核对"数字是否来自用户要的口径"。 |
| 7 | **缓存复用** | Manus：把 KV-cache 命中率称为"生产级 Agent 最重要的单一指标"（缓存输入 $0.3/MTok vs 未缓存 $3/MTok，10× 差），做法是"前缀稳定 + append-only + 确定性序列化"。Wren：`knowledge/sql` 存"已确认的 NL→SQL 对"，`.wren/memory` 用 LanceDB 建索引，成功后可 recall。Dataherald：few-shot 检索 golden SQL。 | **有**：①派发幂等键 memo（含 guidance 摘要，刻意不含 acceptance）；②查询结果缓存 `query:{ds}:{user}:{hash}`（Redis，缺失降级内存）；③上下文的**工具结果块逐条压缩且确定性**（同一结果在任何一步压出同样文本 → 前缀缓存不吃亏，`assembler.py:292-312` 注释明确写了这一点）。 | **缺"经验复用"**：没有"这次成功的 NL→SQL / NL→图表配置"存起来下次检索复用的通道；memo 只在本请求内（`ctx` 级 dict），跨会话无复用。 | ①短期：把 `verify_report_numbers` 通过的 `{goal, tool_chain}` 落一张 `analysis_memo` 表（指纹 → 结果摘要），下次同 fingerprint 直接回放（注意：**只回放"取数+图表配置"，不回放报告文本**，避免话术固化）；②监控：给 `ledger` 加"每步前缀哈希"字段，用"前缀哈希变化次数"当 KV-cache 健康度指标。 |

### 3.1 一句话总结这张表

> **我们的正确性保障是"两头重、中间空"**：输入端安全（4 层 guard）和输出端后验（数字对账 + 两阶段评审）都做了，但**中间"生成前校验 + 结构化错误 + 结果 sanity"这三块是空的**。而这三块恰好是"把偶发错误挡在 LLM 之前"的性价比最高区间。

---

## 4. Lead 循环五维对标与优化建议（必答 3）

> 每维给 2-3 条**可直接改到文件/函数**的建议，并按「收益 ÷ 改动量」排序。标 **[改行为]** 的必须先有评测集，标 **[零行为]** 的可直接合。

### 4.1 上下文（context）

现状事实：每步 `provider.for_agent_loop()` 重建完整 messages；system 由`LEAD_AGENT_SYSTEM` + 6 个状态块拼成；预算表 `enforced=False`（只记账）；工具结果逐条 800 token 压缩。

| # | 建议 | 落点 | 类型 | 理由（对标） |
|---|---|---|---|---|
| C1 | **画布布局不要写回 system 中段，改为"追加式"** | `context/provider.py:refresh_canvas` → 改为把新布局作为一条 `tool_result` 追加（`add_tool_result`），或拼到 system **末尾**的独立块 | [改行为] | Manus 的"前缀稳定 / append-only"是 KV-cache 的第一原则。当前"派发后刷新画布 → system 中部文本变化 → 从该位置起全部前缀失效"，一次派发可能让下一步的输入成本翻倍（长会话更明显）。 |
| C2 | **把 `enforced` 打开，先从 metrics 一个块开始** | `context/budgets.py:116-137`（`enforced=False` → 新增按块的开关）+ `assembler.py:316` | [改行为] | Wren/Manus 都主张"上下文是有限资源、要有预算"。我们现在是"记账但不裁"，等于知道超支却不刹车。建议只对 `METRICS`（易膨胀、可裁剪）先 enforce（如 400 token），datasources/canvas 保持记账。 |
| C3 | **统一截断口径：删掉循环里的 `[:4000]`** | `lead_agent_unified.py:425-427`（event 与 tool message 各截一次）→ 只保留 `assembler.compress_tool_message` 一处 | [零行为，几乎] | `compress.py` 模块头自述"所有截断只允许出现在这里"，实际上工具结果被截两次、单位还不同（字符 vs token）。两处截断会让"同一结果在不同入口长度不同"，评测不可比。 |

### 4.2 工具（tools）

现状事实：10 个工具；工具 JSON schema 固定开销 1947 token（`budgets.py:129`）；观察工具串行执行；`dispatch_analysis` 只有 goal/complexity/acceptance 三个参数。

| # | 建议 | 落点 | 类型 | 理由 |
|---|---|---|---|---|
| T1 | **观察类工具在同一 step 内并发** | `lead_agent_unified.py:307`（for 循环）→ 按 `tool_class == "observe"` 分组 `asyncio.gather` | [零行为] | prompt 已经明确要求"多个只读观察放在同一次回复里一起调用"，但代码是串行 await → 模型并行发 3 个观察，实际耗时仍是 3 次取数；并发后每 step 省 2 次网络往返。 |
| T2 | **Lead 工具面收窄到 6 个，把 `list_fields` / `query_sample` 下沉** | `lead_agent_tools.py:89-100`（OBSERVE_SPECS）+ `QUOTA` | [改行为] | 两个工具的职责都能由"dispatch 内部取数"覆盖（画布编排器已经这么做了：`_EXECUTOR_TOOL_NAMES` 里刻意不放 query_engine）。省 schema token，也减少"看一眼就停"的空转步。**但**：删之前要确认"猜列名"失败率不上升——这正是画布编排器保留 `list_fields` 的原因，建议**保留 list_fields、下沉 query_sample**。 |
| T3 | **给 `dispatch_analysis` 加 `constraints`（数据源 / 时间范围 / 输出形态）** | `lead_agent_tools.py:103-114` → `lead_dispatch.dispatch_analysis` 已有 `constraints` 参数，直接透传 | [零行为] | 现在 Lead 只能用自然语言 goal 表达约束，子任务常"方向对、口径错"。`run_analysis` 的 `constraints` 已经存在（`mode` 就在里面），加字段成本极低，收益是"减少一次重派"。 |
| T4 | **明确"不上沙箱"的理由并写进 ADR** | 无代码落点 | — | 我们已有 DuckDB 内嵌执行 + 参数化 query_engine + 4 层 guard，**沙箱的边际收益主要落在"清洗/特征工程"**。若对话助手未来要以"数据预处理"为定位，再单独立项（Code Interpreter 类），不要在 Lead 循环里塞。 |

### 4.3 可观测（observability）

现状事实：`degradation: list[str]`；ledger 在**循环结束后**才 `trace.update`；decision/intent 事件有，但错误无码。

| # | 建议 | 落点 | 类型 | 理由 |
|---|---|---|---|---|
| O1 | **ledger 每步落 trace，而不是循环末尾一次** | `lead_agent_unified.py:537-543` → 移到循环内每步末尾 | [零行为] | 现在只能看到"整轮总账"，看不到"第几步开始超预算"。Manus/Anthropic 都强调"agent 的上下文是动态曲线"，没有逐步台账就无法优化。 |
| O2 | **degradation 从 list 改成 Counter，done 事件带计数** | `lead_agent_unified.py:156,290,395,403,448,518,531`（多处 append） | [改行为，前端需配合] | 现在"配额打满 3 次"和"1 次"在事件里长得一样，无法判断是"模型不收敛"还是"偶发降级"。建议 `{"lead_agent_quota_dispatch": 2}`。 |
| O3 | **工具错误统一错误码** | `tool_executor` / `query_engine` / `agent_tools.py` 各 error 分支 + `lead_unified` 的 tool_result 事件 | [改行为] | 这是 §3 表中第 4 行（SQL 自修复）策略表的**前置条件**。没有码表，所有"自修复"都只能是 prompt 劝告。 |

### 4.4 失败恢复（recovery）

现状事实：LLM 失败 → 确定性收尾（不重试）；memo 命中 → 返回旧结果（靠 `bypass_memo` 手绕）；重试上限三套不一（5/3/3）。

| # | 建议 | 落点 | 类型 | 理由 |
|---|---|---|---|---|
| R1 | **LLM 调用加一次退避重试，且区分错误类型** | `llm_client.py`（`tool_call_round` / `complete` 外层） | [零行为] | 现在任意异常直接进"确定性收尾"（`lead_agent_unified.py:271-280`），一次上游 5xx 就把整轮降级。区分"上游 5xx（可重试）"与"输出非法 JSON（不可重试，走降级）"是标准做法。 |
| R2 | **"评审未通过"自动触发一次 bypass_memo，而不是靠模型/人工** | `lead_review_stage.run_review_stage` 返回里带 `force_redo=False`；`lead_agent_unified.py:479` 的 `bypass_memo=(already_dispatched or name=="repair")` 改为 `or review_failed` | [改行为] | 现在 `seen_goals` 只识别"同 goal 再派"。若模型换了 goal 措辞但意图相同，会命中旧 memo 得到"未达标"结果，表现为"重做无效"——这正是 `effective_memo_key` 注释里记录的坑。由代码携带状态比让模型记住更可靠。 |
| R3 | **三套重试上限收敛到一张表** | 新增 `agents/retry_policy.py`；`react_agent.MAX_CONSECUTIVE_FAILURES`、`agent_orchestrator._MAX_STEP_RETRIES`、`canvas_orchestrator._MAX_STEP_FAILURES` 引用它 | [零行为] | 同一类错误在三个内核里重试次数不同，用户看到的行为不一致。表驱动在本仓库有成功先例（`lead_worker.py`）。 |

### 4.5 成本与延迟（cost & latency）

现状事实：Lead 循环无总 LLM 预算；`llm.tool_call_round(..., streaming=False)`；每步 1 次 LLM + 派发内部多次 + 评审 0~1 次。

| # | 建议 | 落点 | 类型 | 理由 |
|---|---|---|---|---|
| L1 | **加 `LEAD_AGENT_MAX_LLM_CALLS` 总预算闸** | `lead_agent_unified.py:255` 循环 + 派发前后计数（可复用 `trace` 或新增计数器） | [零行为] | 子内核已有 `_MAX_LLM_CALLS_PER_TASK=24` 的先例，Lead 层没有。现在最坏路径 ≈ 6（Lead 步）×（1 + 子任务 24）= 150 次调用，无上限。 |
| L2 | **首步开启流式，用户先看到字** | `lead_agent_unified.py:264` `streaming=False` → 首步或 `answer` 前一步改 True | [改行为] | 现在整步 LLM 完成后才吐文本，TTFT = 模型整步生成时间。legacy 的 answer 分支是流式的，统一循环反而退化了。 |
| L3 | **给每次派发设"预算档位"** | `lead_agent_tools.py:103-114` 的 `complexity` → 映射到内核的 max_llm_calls / max_steps | [零行为] | simple→react（6 轮），complex→orchestrator（24）现在由内核硬编码决定，Lead 无法表达"这次别太贵"。把 complexity 显式映射成预算，用户/系统可预期。 |

---

## 5. 对话助手 vs 画布助手能否完全解耦（必答 4）

### 5.1 三个方案

**方案 A：共享一个 Lead 循环 + capability profile**

- 一份 `stream_unified` 循环；`CapabilityProfile`（表）决定：system 片段、工具子集、上下文源集合、配额、终止条件、派发内核。
- 改动：新增 `agents/lead/capability.py`（table）+ 拆 `ai_prompts.py:LEAD_AGENT_SYSTEM`（底座 + 两个 profile 片段）+ `provider` 按 profile 选源 + `lead_agent_tools.lead_tool_specs(profile)`。
- 与现状的差距：**很小**。`ENTRY_WORKERS` 已经是这个思路的雏形。

**方案 B：两个独立 Lead 循环，共享执行层与工具层**

- `lead_chat.py` / `lead_canvas.py` 两个 `stream_unified`（或一个文件两个入口函数，各自独立演进）；共享 `TurnContextProvider`、`run_worker`、`Graph` 内核、`event_stream`。
- 改动：中等。循环骨架复制一份（约 500 行），共享层不动；风险是"两个循环各自修 bug"。

**方案 C：完全独立（prompt / 工具 / 事件协议 / 服务各自一套）**

- 改动：大。`event_stream` 已经是与入口无关的通用 Redis Stream（**这是唯一"共享成本很低"的部分，拆开纯亏**），`chat_task` / `canvas_task` 已分，但工具层与执行层共享面很大。

### 5.2 打分（1-5，**分数越高越好**；权重为建议值，可调）

| 维度 | 权重 | A 共享循环+profile | B 两循环共享执行层 | C 完全独立 |
|---|---|---|---|---|
| 代码改动量（越小越高分） | 30% | **5**（加表 + 拆 prompt） | 3（复制循环骨架） | 1（三套都要维护） |
| 行为一致性风险（越低越高分） | 25% | 3（profile 分支易漂移，但只要"表 + 单测锁住 profile 差异"可控） | **4**（各自演化，风险局部化；不一致是显式的） | 2（用户会在两个页面看到不同性格/不同话术） |
| 教学可解释性 | 15% | **4**（"一个循环 + 一张能力表"最好讲；也是面试可讲点） | 3（两个循环要讲两遍） | 2（"为什么有两套"难自圆其说） |
| 运维成本（越低越高分） | 20% | **5**（一处修 bug 两处生效） | 3 | 1 |
| 未来加第三入口（报表/移动端） | 10% | **4**（加一行 profile） | 3（再加一个循环） | 2（再加一套服务） |
| **加权总分** | | **4.25** | **3.25** | **1.50** |

### 5.3 推荐结论

> **现在选 A，并把 A 做成"可平滑长成 B"的形状。**

三条必须同时满足的条件（否则 A 会退化成"一份 prompt 里堆两套规则的现状"）：

1. **prompt 必须拆**：`ai_prompts.py` 拆成 `LEAD_CORE`（角色/权限/终止/表达风格）+ `CHAT_PROFILE`（分析/清洗/对账规则）+ `CANVAS_PROFILE`（构图/落块/KPI 卡片/布局规则）。当前 `LEAD_AGENT_SYSTEM`（`ai_prompts.py:707-778`）里 KPI 卡片、清空画布、落块顺序等画布规则占了相当篇幅，对话入口也在读它们——这是已经存在的成本。
2. **profile 必须是数据不是分支**：`CapabilityProfile` 用 dataclass/表，**禁止** `if entry == "canvas"`。可加单测："两个 profile 的工具集差集 == 预期集合"，防止漂移。
3. **差异必须有各自专属的"不可妥协项"**：画布的终止条件是"必须产出 canvas_action"（`canvas_orchestrator` 已有此校验），对话的终止条件是"回答 + 数字后验通过"。这两条若共用一套判定，必然两头不讨好。

**什么时候切到 B**：当出现以下任一信号时——① 两个入口的 system prompt 各自超过 2000 token 且重叠 < 50%；② canvas 侧出现"图状态机"需求（多轮改图、撤销/重做、中断恢复）；③ 对话侧引入沙箱执行（工具集差异变成"完全不同类"）。此时 B 的"显式不一致"反而变成优点。

**不推荐 C 的理由（除分数低外）**：`event_stream.py` 已经把"可回放事件日志"做成了与入口无关的基础设施，`confirm_request`/`ui_action confirm` 的 HITL 协议也是共用的。完全独立意味着这两块要复制，而它们恰好是最容易出"刷新丢事件""确认卡片不弹"这类线上问题的地方。

---

## 6. 双助手定位差异清单（必答 5 · 可直接落成配置表）

> 定位前提（来自 Lead 的任务定义）：**对话助手 = 复杂分析 + 数据预处理 + 清洗**；**画布助手 = 搭建 + 洞察可视化**。
> 下表每一行都应是一行配置，而不是 `if`。第二列/第三列即 `CapabilityProfile` 的字段值。

### 6.1 主表

| 维度 | chat.analysis（对话助手） | canvas.build（画布助手） | 现有落点 / 改法 |
|---|---|---|---|
| **profile id / 主责** | 取数、复杂分析、数据预处理与清洗建议；答案以**文本报告**交付 | 构图、落块、布局与洞察呈现；答案以**画布块**交付 | 新增 `agents/lead/capability.py` |
| **prompt 组成** | `LEAD_CORE` + `CHAT_PROFILE`（分析步骤、口径优先、数字后验、清洗流程） | `LEAD_CORE` + `CANVAS_PROFILE`（报告骨架、KPI 卡片单指标、布局规则、破坏性操作确认） | 拆 `ai_prompts.py:LEAD_AGENT_SYSTEM`（707-778） |
| **Lead 工具集** | observe: get_canvas_layout（弱相关可去）、list_datasources、list_fields、list_metrics；dispatch: dispatch_analysis、repair；terminal: answer/ask_user/stop | observe: get_canvas_layout、list_datasources、list_metrics；dispatch: dispatch_analysis、repair；terminal: answer/ask_user/stop | `lead_agent_tools.lead_tool_specs(profile)` |
| **观察配额** | observe 2 | observe 3（画布状态变化快，需多次确认"我刚落的块长什么样"） | `lead_agent_tools.QUOTA`（当前 `{observe:2, dispatch:3, repair:2}`） |
| **派发内核（entry × complexity）** | complex → orchestrator；simple → react | complex → canvas；simple → react | `lead_worker.ENTRY_WORKERS`（已是表） |
| **dispatch 参数形态** | `goal` + `complexity` + `acceptance{require_verified, min_rows?}` + `constraints{source, time_range, output:report}` | `goal` + `complexity` + `acceptance{min_blocks, require_verified}` + `constraints{layout, chart_types}` | `lead_agent_tools.DISPATCH_SPECS`（现仅 goal/complexity/acceptance） |
| **上下文源（注入 system 的块）** | memory（四节结构）、window（`LEAD_CTX_ANSWER_HISTORY_TOKENS=2000`）、metrics、datasources、subtask、assessment、artifact；**可选**：数据质量摘要（`data_quality` 的 6 类检测）、清洗建议 | memory、window、metrics、datasources、**canvas layout（每步 force 刷新）**、artifact（防重复出同类型图）、subtask、assessment | `TurnContextProvider.for_turn` 按 profile 选 Source 集合 |
| **上下文预算（目标值）** | system 1200 / metrics 400 / memory 1333 / window 2000 / tool_result 800 | system 1200 / canvas 300 / artifact 300 / memory 800 / window 1200 / tool_result 800 | `context/budgets.py` 新增 `TARGET_CHAT` / `TARGET_CANVAS` |
| **循环终止条件** | ① `answer` 且数字后验通过；② `stop`；③ 步数 6；④ 总 LLM 预算（建议 12） | ① 每步**必须**产出 canvas_action（纯文本收尾 = 未达成，`canvas_orchestrator` 已有此校验）；② `stop`；③ 步数 6；④ HARD 零落块 = fail | `lead_agent_unified.stream_unified` + profile 字段 |
| **产物类型** | 报告文本（`report` 事件）、洞察卡片、清洗方案（`clean_suggest`）、可选图表（`render_chart` 只出 option，不落块） | `canvas_action` 事件（add/update/remove/arrange）、块列表、布局、一句总结 | `event_stream` 事件类型按 profile 白名单 |
| **失败重试策略** | react：连败 5 熔断 / 查询累计 5 次上限 / 单轮并发 3；planner：schema 校验失败回喂重试 1 次；tool hint 重试 ≤3 | canvas：单步重试 3 / 单步工具调用 4 / 单步超时 45s / 同签名 3 次跳过；`add_chart_block` 失败按 error message 修正后重试 ≤2（prompt 约束） | `retry_policy.py`（新） |
| **HITL 触发** | 破坏性操作（清空/删除）→ `ask_kind=confirm`；信息不足 → `clarify` | 同上 + **覆盖已有块**（update 已存在的 block）建议也走 confirm | `lead_agent_unified.effective_ask_kind`（已有代码闸，复用） |
| **事件协议** | `decision/intent/tool_call/tool_result/progress/report/confirm_request/done` | 同上 + `canvas_action` | `event_stream`（已通用，不需按入口分） |
| **可观测重点** | 每指标的数字后验通过率、重派率 | 落块成功率、构图步骤失败签名分布、布局冲突率 | `observability.py` + ledger |

### 6.2 可直接抄的配置骨架（Python dataclass，零依赖）

```python
# app/services/agents/lead/capability.py（新增）
from dataclasses import dataclass, field

@dataclass(frozen=True)
class CapabilityProfile:
    name: str                       # "chat.analysis" | "canvas.build"
    prompt_fragments: tuple[str, ...]   # ("LEAD_CORE", "CHAT_PROFILE")
    lead_tools: frozenset[str]          # 工具名子集（可测：与画布集的差集 == 预期）
    quota: dict[str, int]               # {"observe": 2, "dispatch": 3, "repair": 2}
    context_sources: tuple[str, ...]    # ("memory","window","metrics","datasources","subtask","assessment")
    budgets: str                        # "TARGET_CHAT" | "TARGET_CANVAS"
    terminal: dict                      # {"require_canvas_action": False, "require_verified": True}
    acceptance_schema: frozenset[str]   # {"require_verified"} | {"min_blocks","require_verified"}
    max_steps: int = 6
    max_llm_calls: int = 12

PROFILES: dict[str, CapabilityProfile] = {
    "chat": CapabilityProfile(
        name="chat.analysis",
        prompt_fragments=("LEAD_CORE", "CHAT_PROFILE"),
        lead_tools=frozenset({"list_datasources","list_fields","list_metrics",
                              "dispatch_analysis","repair","answer","ask_user","stop"}),
        quota={"observe": 2, "dispatch": 3, "repair": 2},
        context_sources=("memory","window","metrics","datasources","subtask","assessment","artifact"),
        budgets="TARGET_CHAT",
        terminal={"require_canvas_action": False, "require_verified": True},
        acceptance_schema=frozenset({"require_verified"}),
    ),
    "canvas": CapabilityProfile(
        name="canvas.build",
        prompt_fragments=("LEAD_CORE", "CANVAS_PROFILE"),
        lead_tools=frozenset({"get_canvas_layout","list_datasources","list_metrics",
                              "dispatch_analysis","repair","answer","ask_user","stop"}),
        quota={"observe": 3, "dispatch": 3, "repair": 2},
        context_sources=("memory","window","metrics","datasources","canvas","artifact","subtask","assessment"),
        budgets="TARGET_CANVAS",
        terminal={"require_canvas_action": True, "require_verified": True},
        acceptance_schema=frozenset({"min_blocks","require_verified"}),
    ),
}
```

> **配套单测**（防漂移）：`test_capability_profiles.py` 断言 ① 两个 profile 的 `lead_tools` 差集 == 预期集合；② `lead_tools` 与 `WRITE_TOOL_NAMES` 交集为空（沿用现有 `test_lead_p1_agent_tools.py` 的断言风格）；③ `context_sources` 里用到的每个 Source 都在 `provider._sources` 中有实现。

---

## 7. 迁移路线（建议按此顺序，每步可独立回滚）

| 阶段 | 内容 | 验收标准 | 回滚 |
|---|---|---|---|
| **P0（1 周，零行为）** | ① 删 `lead_agent_unified.py:425` 的 `[:4000]`；② observe 并发；③ ledger 逐步落 trace；④ degradation 改 Counter；⑤ LLM 调用退避重试 | 全量测试通过；同一批问题集的 token/时延不劣化 | 纯代码回滚 |
| **P1（2 周，表驱动）** | ① 拆 prompt 为 `LEAD_CORE` + 两个 profile；② 新增 `capability.py`；③ `lead_tool_specs(profile)`；④ 加 `test_capability_profiles.py` | 两个入口行为逐字对比（对拍集）与拆分前**一致**；profile 单测锁死 | 保留旧 `LEAD_AGENT_SYSTEM` 常量做开关 |
| **P2（3 周，补正确性）** | ① 错误码表；② `repair_policy.py`；③ 结果 sanity 三条 HARD；④ `dry_plan` 工具 | 重派率下降（目标 -20%）；"空结果/全 NULL"类失败从 SOFT 变 HARD 可判 | 每条独立开关 |
| **P3（按需）** | ① `analysis_memo` 跨会话复用；② 指标检索 top-k；③ 沙箱评估（若对话侧确要"数据预处理"定位） | 有评测集才做，**禁止裸上** | — |

---

## 8. 我最不确定的 2 点（需要 Lead / 其他调研员补数据）

1. **单循环 vs legacy 的真实差距缺数据。**
   `lead_agent_unified.py:56-76` 的注释给了"20 题双跑：react 派发 63% vs legacy 37%、多 47 次 LLM 调用（+32%）"，但这只是**小样本 + 单轮**的对拍，没有 P95 时延、没有失败率、没有长会话成本。**我没有跑评测（按约束不能跑全量 pytest），所以本文所有"成本/收益"判断都是基于代码结构的推断，不是测量结果。**
   *消除方法*：用现有的 `trace` + `agent_loop_ledger` 各跑 30-50 题两遍，产出"派发分布 / LLM 调用数 / P50-P95 时延 / degradation 分布"四张表。

2. **"抄 Wren AI 的 schema linking"对我们可能收益很小。**
   依据：我们每个数据源在 DuckDB 里是**单 schema 单表 `data`**（`query_engine.py:50-65` 明确从 `table_name='data'` 取列），**没有 join 路径、没有多表关系、没有 MDL 的 relationships/cubes**。Wren 的 schema linking 解决的是"多表大 schema 里选对子集"，我们面对的是"单宽表里选对列"。但**我没有字段基数的分布数据**（不知道单表字段数是 20 还是 200），所以无法判断 `list_fields` 全量 dump 到底吃掉多少 token。
   *消除方法*：跑一条 SQL 统计各数据源 `schema_meta.fields` 长度分布；若 P90 < 40 列，则 schema 检索优先级降到最低，把预算全投到 §3 的 dry-plan / 错误码 / sanity 三件套。

> 另外两个"低置信但值得记录"的判断：
> - Julius AI 的具体编排细节**没读到一手文档**（官网被 Cloudflare 拦截），本文只在"沙箱代码执行"范式里用它当例子，且标了 [二手]；若要做"对话助手引入沙箱"的决策，需重新调研。
> - Chat2DB 同样未读到一手架构文档，本文未对其做任何具体论断。

---

## 9. 来源

**一手文档 / 源码（本轮实际读取）**

1. Wren AI Docs — Architecture（correctness 7 pillars、四层架构、CLI primitives、dry-plan/dry-run、memory/LanceDB）：<https://docs.getwren.ai/oss/reference/architecture>
2. Wren AI Docs — How Wren AI works（UI / AI Service / Core 三分、请求流）：<https://docs.getwren.ai/oss/overview/how_wrenai_works>
3. Anthropic Engineering — *Building effective agents*（workflow vs agent 的区分、"先用最简单方案"、prompt chaining/routing/orchestrator-workers 模式）：<https://www.anthropic.com/research/building-effective-agents>
4. Anthropic Engineering — *Effective context engineering for AI agents*（context rot、注意力预算、最小高信号 token、just-in-time 检索）：<https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
5. Anthropic / Claude — *Building agents with the Claude Agent SDK*（`gather context → take action → verify work → repeat`、"给 Agent 一台电脑"）：<https://claude.com/resources/articles/building-agents-with-the-claude-agent-sdk>
6. Manus — *Context Engineering for AI Agents: Lessons from Building Manus*（KV-cache 命中率是第一指标、前缀稳定、append-only、文件系统作外部记忆）：<https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus>
7. LangChain Docs — LangGraph Persistence（checkpointer：会话连续性 / HITL / time travel / 容错；store：跨线程长期记忆）：<https://docs.langchain.com/oss/python/langgraph/persistence>
8. PyPI — `langgraph-supervisor`（supervisor 模式；官方注记：多数场景建议直接用 tool-calling 手动实现 supervisor 以获得更好的上下文控制）：<https://pypi.org/project/langgraph-supervisor/>
9. Dify — *Agent Node Introduction*（Workflow 中嵌 Agent Node；Node=执行单元 / Strategy=可插拔决策逻辑（ReAct、Function Calling）；树状推理日志）：<https://dify.ai/blog/dify-agent-node-introduction-when-workflows-learn-autonomous-reasoning>
10. DB-GPT Docs — AWEL（operator / AgentFream / DSL 三层，DAG 编排，"让围绕数据的编程更确定"）：<http://docs.dbgpt.cn/docs/awel/>
11. LangChain Blog — *How Dataherald Makes Natural Language to SQL Easy*（两个 LangChain agent：RAG-only 与 fine-tuned LLM-as-a-tool；schema-linking tool、SQL 执行校验工具、few-shot retriever；"评估 AI 生成 SQL 极难"）：<https://www.langchain.com/blog/dataherald>
12. PandasAI Docs — Agent（多轮、clarifications/explanations/optional sandboxed execution、本地向量库训练）：<https://docs.pandas-ai.com/v3/agent>
13. Vanna PyPI（2.0）（RAG/工具化 agent、streaming 结构化组件、user-aware 行级权限、audit log）：<https://pypi.org/project/vanna/>

**本仓库代码（本文"事实"栏全部以此为准）**
- `backend/app/services/agents/lead/{lead_agent_unified,lead_agent_tools,lead_worker,lead_dispatch,lead_review_stage,lead_tools,lead_observe}.py`
- `backend/app/services/context/{provider,assembler,budgets,compress}.py`、`context/sources/{state,memory,window}.py`
- `backend/app/services/{agent_tools,canvas_tools,sql_guard,sql_guard_ast,query_engine,data_quality,metric_service,event_stream,ai_prompts}.py`
- `backend/app/services/agents/{graph,react_agent,agent_orchestrator,canvas_orchestrator,planner_agent,tool_executor}.py`
- `backend/app/config.py`（`LEAD_AGENT_MODE`/`LEAD_AGENT_MAX_STEPS`/`TOOL_RESULT_MAX_TOKENS` 等默认值）

**二手 / 未读到一手（本文已降级使用）**
- Wren AI PR #2311 标题（"AI systems should not have unlimited freedom over every object in a warehouse"）——来自搜索结果标题，未读全文。
- Julius AI（官网 Cloudflare 拦截）、Chat2DB（未读架构文档）——本文未对其编排细节做论断。
- E2B / Code Interpreter 类沙箱——按通用工程共识描述，未引具体实现文档。

