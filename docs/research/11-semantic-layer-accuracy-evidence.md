# 11 · 用数字说话：语义层/指标层对 text-to-SQL 准确率的实际影响，以及 Agent 在 SQL 受限时的行为代价

> 调研员：research-agent ｜ 日期：2026-11-09 ｜ 范围：公开基准与厂商实测的准确率数字 · "不会写 SQL" vs "不知道口径" 的分离度量 · 覆盖率×准确率 · 被拒绝后的重试代价 · SQL 错误类型统计
>
> **标注约定**
> - **[一手]** = 论文原文 / 官方文档 / 官方代码仓库，本地已抓取正文并核对数字；
> - **[厂商自测]** = 厂商自己跑的 benchmark，方法与样本可查但存在利益相关，**默认不可横向比较**；
> - **[二手]** = 第三方转述或转引，未读到原始出处，可信度降级；
> - **[未证实]** = 检索范围内没有找到公开数据，**不要当作事实引用**。
> - **可比较性分级**：**A** = 同基准同指标可直接比；**B** = 方向可比、条件不同；**C** = 厂商内部集/私有数据，**不可跨表比较**。
>
> 本文所有"我们/本仓库"均指 `E:\BI\LvcoBI\lvco-bi` 当前 HEAD。**本文只做调研，未修改任何代码、未运行任何测试。**

---

## 0. 一页结论（先给能拿去用的）

1. **"模型不会写 SQL"在公开基准上已经不是主要矛盾**：Spider 1.0 上同类代码 agent 已到 **91.2%**，BIRD 上最好方法到 **71.83%**（2024），而人类在 BIRD 上是 **92.96%**。模型写 SQL 的"语法能力"基本够用。[一手，2024–2025]
2. **一旦换到企业真实数仓，准确率断崖式下降**：Spider 2.0（632 题，BigQuery/Snowflake）上 o1-preview 代码 agent 只有 **21.3%**；BEAVER（9128 题、812 表、私有数仓）上 GPT-5.2 agent **10.8%**、ReFoRCE+Claude-4.5 **11.4%**——同一方法在 Spider 2.0 上是 **62.9%**。**降幅不是模型差异，是"数据/口径/结构"差异**。[一手，2024–2026]
3. **语义层提升的量级是 +15~+25 个百分点，不是 2~3 倍**，且强依赖"怎么测"：
   - Snowflake（厂商自测，BIRD 四个子集，Claude 3.5 Sonnet）：**57% → 78%（+21pp）**；
   - dbt Labs（厂商自测，11 题×20 次）：补 3 个 model 后 **SL 98.2%/100% vs 自由 SQL 90.0%/84.1%**；补 model 前是 **72.7% vs 64.5%**；
   - Cube（厂商自测，100 题，**只加 4KB 口径 markdown、不给编译器**）：**+17.2 / +22.2 / +23.2 pp**。
4. **口径知识的收益可以被"纯 context"单独隔离出来**：Cube 的对照实验是同一模型、同一题、同一单轮协议，唯一变量是那段 4KB 的口径文档，三个前沿模型同时 +17~23pp，且**加了口径后三个模型彼此不可区分（67.7–68.7%）**。这是目前最干净的"不知道口径"证据。[一手，2026]
5. **但厂商实验普遍把"给了口径"和"不让模型写 SQL"混在一起**：dbt 的 SL 臂根本不生成 SQL（MetricFlow 编译），Snowflake 的 Cortex Analyst 也是编译器 + 语义模型。**所以"+21pp"不能归因给"口径"或"编译"中的任何一个**。这个混淆已被公开批评，引用时必须带上。[二手评论，2026]
6. **覆盖率是语义层的硬边界，而且它是"拒绝"不是"答错"**：dbt 的 11 题里有 3 题超出 MetricFlow 跳数，SL 准确率 **0.0%**，自由 SQL 反而 **70%–100%**；补 3 个 dbt model 后 SL 到 **98.2%–100%**。**语义层答不了会说"答不了"，自由 SQL 答不了会给你一个像模像样的错数**——这是选型时最该抄的一句话。[一手，2026]
7. **Agent 被工具/护栏拒绝后的代价，公开基准里没有直接测量（[未证实]）**。能得到的最接近的三组代理数据：
   - τ-bench：gpt-4o 单次成功率 ~61%（retail）/ ~35%（airline），但 **pass^8 掉到 ~25%**——说明重试并不收敛，是"每次都重新掷骰子"；
   - 自修复经济学（ICLR'24）：把预算堆在"修"上而不是"重采样"上，GPT-4/APPS 只有 **1.05×**（甚至 **0.97×**）；而把反馈质量从"模型自评"换成"人类反馈"，修复成功率 **33.3% → 52.6%（1.58×）**；
   - Anthropic 工程博客（二手转述）：单 agent 约 **4×** chat token，多 agent 约 **15×**。
   结论：**重试的收益几乎全部由"错误信息的质量"决定，而不是由"重试次数"决定**——这直接对应我们 `ast_full_check` 返回的一句话中文错误。
8. **SQL 错误类型已有可引用的量化分类**（BEAVER 采样 275 条错例）：**结构/分析错误 46.8%**（其中 GROUP BY/ORDER BY/HAVING 位置错 **22.8%**、窗口/聚合/标量函数用错 **14.2%**、查询分解 7.7%、DISTINCT/LIMIT 行选择 2.1%）｜**schema linking 35.2%**（表 11.8%、列 12.5%、JOIN 条件/类型 10.9%）｜**谓词 18.0%**（字面量错 7.4%、漏谓词 10.6%）。这和我们"聚合粒度/去重/时间范围/JOIN 基数"的判分器清单几乎一一对应。[一手，2026]
9. **判分器本身是最大噪声源**：BIRD Mini-Dev 的标注错误率被实测为 **52.8%**，Spider 2.0-Snow 为 **62.8%**；用修正后的 100 条重跑 16 个开源 agent，分数变化 **−7%~+31%（相对）**，排名变化 **±9 位**。我们自己 `judge.py` 用"SQL 关键字重叠 ≥0.6"判对错，属于同一类问题——**在换判分器之前，任何 A/B 都在测判分器**。[一手，2026]
10. **一句话给决策者**：把"语义层 vs 自由 SQL"做成 **paired A/B（同题、同模型、同判分器、只换口径来源）**，同时记录 **blocked_rate / retries_per_task / tokens_per_success**；否则我们永远只能引用别人的 +20pp，而无法回答"我们自己的口径治理值多少"。

---

## 1. 公开基准：地板、天花板与"饱和"

| 基准 | 指标 | 数值 | 条件 | 年份 | 可比较性 | 来源 |
|---|---|---|---|---|---|---|
| Spider 1.0 | Exact Match | 12.4%（当时最好模型） | 200 DB / 10,181 题 / 138 域，跨库 split | 2018 | A | [arXiv:1809.08887](https://arxiv.org/abs/1809.08887) |
| Spider 1.0 | 执行准确率 | **91.2%**（代码 agent 框架） | Spider 2.0 论文自述的对照点 | 2024 | B（"91.2%"是转述，非该论文主指标） | [arXiv:2411.07763](https://arxiv.org/abs/2411.07763) |
| BIRD | 执行准确率 EX | ChatGPT **40.08%**；人类 **92.96%** | 12,751 对 / 95 DB / 33.4GB / 37 域 | 2023 | A | [arXiv:2305.03111](https://arxiv.org/abs/2305.03111) |
| BIRD | 执行准确率 EX | **71.83%**（当时榜首） | 放弃 schema linking，全 schema 进上下文 + 增强/选择/纠错 | 2024 | A | [arXiv:2408.07702](https://arxiv.org/abs/2408.07702) |
| Spider 2.0 | 执行准确率 EX | **21.3%**（o1-preview 代码 agent） | 632 题，BigQuery/Snowflake，单题常 >100 行 SQL | 2024 / ICLR'25 | A | [arXiv:2411.07763](https://arxiv.org/abs/2411.07763) |
| BEAVER | 执行准确率 EX | **10.8%**（GPT-5.2 agentic）/ **11.4%**（ReFoRCE + Claude-4.5） | 9,128 对 / 812 表 / 19 域，**私有企业数仓查询日志** | 2024，v3 2026 | A | [arXiv:2409.02038](https://arxiv.org/abs/2409.02038) |
| BEAVER | 执行准确率 EX | **30.1%**（给定全部子任务 oracle 标注） | 同上，上界探针 | 2026 | A | 同上 |
| BEAVER | 对照 | 同一 ReFoRCE 方法在 Spider 2.0 上 **62.9%** | 同方法、跨基准 | 2026 | B（跨基准，但同方法同模型，参考价值高） | 同上 |

**怎么读这张表（面试安全版）**：
- Spider 1.0 已经**饱和**，任何"我们让 text-to-SQL 到 90%"的说法都不构成能力证明。
- BIRD 与 Spider 2.0 之间是**口径/结构复杂度**的差异，不是模型代际差异。
- BEAVER 的 10.8% 是**目前公开可得的最低"企业真实"数字**，它同时说明：给 oracle 子任务提示也只到 30.1%，**剩余 ~70% 的失败来自"子任务之外"**（高级函数、谓词、语义）。
- **不要**把 BEAVER 的 10.8% 说成"GPT-5.2 只会写 10% 的 SQL"——它是"在私有数仓+真实查询日志上端到端做对 10.8%"。

---

## 2. 语义层/指标层到底提升多少：四个厂商实验的完整条件

| 来源 | 年份 | 语义层臂 | 对照臂 | 题目/规模 | 结果 | 可比较性 |
|---|---|---|---|---|---|---|
| Snowflake Cortex Analyst | 2024-08 | Cortex Analyst（agent + 语义模型） | GPT-4o 单 prompt 写 SQL | **内部 150 题**（过滤/聚合/趋势三级） | Cortex **90%+**；GPT-4o **51%**；"约为单 prompt 的 2 倍" | **C**（内部集，不可与 BIRD 比） |
| Snowflake 语义模型自动化 | 2025-03 | Cortex Analyst + agent 自动改语义模型 | 同一 Claude 3.5 Sonnet，纯 schema+外部知识 prompt | BIRD dev 的 4 个子集，**60/80/111/141 题** | **57% → 78%（+21pp）**；分库 52→83、63→80、45→70、69→79（"平均 +20%"） | **B**（同题同模型，但判分用 LLM judge） |
| dbt Labs | 2026-04 | dbt Semantic Layer（MetricFlow 编译，模型**不写 SQL**） | 自由 text-to-SQL（全 schema DDL 进上下文） | **11 题 × 20 次**，ACME Insurance（15 表，~68KB） | 补 3 个 model 后：SL **98.2%**（Sonnet 4.6）/ **100%**（GPT-5.3 Codex）vs T2SQL **90.0% / 84.1%**；补 model 前：**72.7% vs 64.5%** | **B**（题量太小；且 SL 臂任务不同） |
| Cube | 2026-04 | schema + **4KB 手工口径 markdown**（无编译器、无工具调用） | 仅 schema DDL | **100 题**（Contoso Retail / ClickHouse），单轮 | Opus 4.7 **50.5→67.7**；Sonnet 4.6 **46.5→68.7**；GPT-5.4 **45.5→68.7**（+17.2/+22.2/+23.2pp，McNemar p≤0.0015，n=99） | **B**（单数据集；但对照最干净） |
| Databricks | 2025–2026 | Genie（受治理表 + 指标定义 + 示例问题） | 通用 NL 接口 | 无公开数字 | 只给立场："执行成功 ≠ 答案正确；准确率是特定工具+数据范围+题集的属性" | **C**（无数据） |

**必须同时说出来的三个限定条件**（否则就是替厂商做广告）：

1. **混淆变量：context vs compiler**。Snowflake 与 dbt 的语义层臂同时改变了"给多少口径"和"还让不让模型写 SQL"。dbt 的 SL 臂根本不产出 SQL，是"选指标 → MetricFlow 编译"，严格说**不是同一个任务**。公开批评见 [The Semantic Layer Benchmark Problem](https://blog.strata.do/p/the-semantic-layer-benchmark-problem)（2026，观点文，非同行评审）。[二手]
2. **只有 Cube 隔离了 context**：同一模型、同一题、同一单轮协议，只多一段文档。所以**"口径知识本身值 +17~23pp"这句话可以放心引用；"语义层值 +20pp"这句话不能**。
3. **样本量**：dbt 是 11 题×20 次（同一个模型重复采样，方差被低估）；Cube 100 题；Snowflake 2025 是 4 个子集共 392 题、判分用 LLM judge（Mistral Large 2）。**没有一个达到 Spider/BIRD 的公开规模**。

---

## 3. 关键分辨：失败是"不会写 SQL"还是"不知道口径"

### 3.1 三组能分离归因的证据

| 证据 | 设计 | 数字 | 说明 |
|---|---|---|---|
| **BEAVER 五子任务 + oracle hints** | 把端到端拆成 多表检索 / JOIN key 检测 / 列映射 / 领域知识抽取 / 查询分解，逐项给人工标注 | 端到端 **10.8%** → 给全部标注 **30.1%** | 说明"子任务没解决"是主瓶颈；但即使全给，仍有 70% 失败 → **子任务之外还有结构化/函数/谓词问题**。[一手] |
| **BEAVER 分类题集（RQ2）** | 三类题：①只有结构复杂度 ②只有领域知识、结构简单 ③两者都有 | 三类都差，**①（纯结构）比②（纯领域知识）更难**；③ 最差 | **重要反直觉**：不能简单说"口径是唯一瓶颈"，**SQL 结构能力在企业场景里仍然更贵**。[一手] |
| **Cube paired（context-only）** | 同模型同题，唯一变量是 4KB 口径文档 | **+17~23pp**，且加文档后模型间差异消失 | **"不知道口径"可以单独测出来，且量级 20pp。**[一手] |
| **Spider 2.0 oracle 函数文档** | 直接把所需方言函数文档塞进 prompt | DAIL-SQL+GPT-4o：给文档 **5.85%**、不给 **5.68%**，差异在噪声内（作者结论："轻微提升"，模型已能选对函数，缺的是"用对"） | **"会语法"≠"会用对"**：模型缺的不是 API 名称，是"这个业务问题该用哪个函数、按什么口径算"。[一手] |

### 3.2 一个可以拿去讲的判断

> **在公开基准上，"不会写 SQL"已经被解决到 90% 量级；在企业场景里，失败重新分裂成三份：结构（~47%）、schema/join（~35%）、谓词与口径（~18%）。语义层/口径治理直接消灭的是第三份，并顺带压缩第二份里的 join 部分；它不解决第一份。**

这与我们的现状一致：我们的 `metric` formula 只允许 8 个聚合 + 四则 + `metric("key")`，本质上是**把第一份（结构）也一起锁死**，换来的是"永远不会写错结构"，代价是**覆盖率**（见 §4）。

---

## 4. 覆盖率 × 准确率：公开数据只有一条曲线

| 观察 | 数字 | 出处 |
|---|---|---|
| 覆盖率不足时，语义层不是"降级"，是"归零" | dbt 11 题中 3 题超出 MetricFlow 跳数 → **SL 0.0%**，自由 SQL **70%（Sonnet）/100%（Codex）** | [dbt 2026](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026) |
| 覆盖部分的准确率可以到顶 | 同一实验：覆盖的 8 题 **SL 100% vs T2SQL 62.5%/51.2%** | 同上 |
| 补 27% 的问题覆盖，整体从 72.7% → 98.2% | 用 LLM 生成 **3 个 dbt model** 后，11 题全部可被 SL 回答 | 同上 |
| 企业内集上的"全域覆盖"数字 | Snowflake 内部 150 题，语义模型覆盖全部题 → **90%+** | [Snowflake 2024](https://www.snowflake.com/en/blog/engineering/cortex-analyst-text-to-sql-accuracy-bi/) |
| **连续覆盖率-准确率曲线** | **没有公开数据** | **[未证实]** |

**结论**：公开材料只支持"**分段**结论"——覆盖到的问题接近 100%，覆盖不到的问题接近 0%，整体准确率 ≈ 覆盖率 × 覆盖内准确率。**没有任何论文/博客给出 coverage 从 10% 拉到 90% 时准确率如何变化的曲线**。这条曲线必须我们自己测（见 §8 建议 5/7）。

> 补充：**语义层的第二个收益是"确定性"，不是"准确率"**。SQLStructEval（2026）在 Spider 上对每题采样 10 次（每模型 10,340 条）：GPT-5-mini 平均 **1.913 种**不同 AST 结构、最常见结构只占 **65.0%**；Claude-4.5-Opus 也有 **0.779 种 / 68.7%**。即"同一问题跑两次，结构就不一样"。[一手，[arXiv:2604.06736](https://arxiv.org/abs/2604.06736)] 这与我们"同一问题两次给不同数字"的体感一致，且它**不体现在单次准确率里**。

---

## 5. Agent 在 SQL 被拒绝时的行为代价

### 5.1 直接结论

**没有任何公开 benchmark 测量"SQL 被安全策略/护栏拒绝"后的 token/延迟放大倍数。** 检索了 τ-bench、AgentBench、SWE-bench 系、MAST、自修复文献，均无此切片。**这一条必须标 [未证实]，不能编数字。**

### 5.2 可引用的代理数据（按可用性排序）

| 代理指标 | 数值 | 条件 | 对我们的含义 | 来源 |
|---|---|---|---|---|
| 重试不收敛（一致性塌陷） | gpt-4o **pass^1 ≈ 61%（retail）/ 35%（airline）**，**pass^8 ≈ 25%（retail）** | 函数调用 agent，8 次独立试验 | 同一任务多跑几次，"每次都成"的概率掉到 25% → **重试不是"多试几次就对"，而是重新掷骰子** | [τ-bench, 2024](https://arxiv.org/abs/2406.12045) |
| 修复预算的边际收益 | GPT-4/APPS：10 次采样 + 每条修 1 次（共 20）→ 仅 **1.05×** pass@20；2 次采样 + 每条修 10 次（共 22）→ **0.97×**（**不如**直接采样 22 次） | 等算力预算对比 | **把预算堆在"修"上是负收益**，除非反馈质量提升 | [Is Self-Repair a Silver Bullet?, ICLR'24](https://arxiv.org/abs/2306.09896) |
| 反馈质量的杠杆 | 用人类反馈替换 GPT-4 自评，被修复程序通过率 **33.3% → 52.6%（1.58×）** | 40 个失败程序，16 名程序员 | **同样的重试次数，错误信息质量决定 1.58 倍差异** → 我们 `ast_full_check` 的那句"不允许使用 UNION 操作"就是低质量反馈 | 同上 |
| 无外部反馈时自我纠正无效 | 内在自我纠正"struggle"，有时**性能下降** | 推理任务 | 不要让 agent 在无新信息时"再想想" | [LLMs Cannot Self-Correct Reasoning Yet, 2023](https://arxiv.org/abs/2310.01798) |
| token 放大基线 | 单 agent ≈ **4×** chat；多 agent ≈ **15×** chat | Anthropic 生产系统自述（原文页面为 JS 渲染，本次经第三方摘要核对，**[二手]**） | 我们 Lead 循环 + 内核派发已接近多 agent 形态，**预算应按 15× 设计** | [Anthropic, 2025](https://www.anthropic.com/engineering/multi-agent-research-system) |
| 干预的天花板 | 针对失败模式做 prompt/流程干预，ChatDev 任务完成率仅 **+14%**，且"未解决全部失败" | MAST 1600+ 轨迹、14 种失败模式、3 大类（κ=0.88） | **加一句 prompt 提示的期望收益是个位数到十几个点，不是翻倍** | [MAST, 2025](https://arxiv.org/abs/2503.13657) |

### 5.3 我们可以自己算的放大公式（建议写进埋点）

```
R = 1 + p_block × E[retries | block] × (C_round / C_task)
```

- `p_block`：被 `ast_full_check` / 意图闸拦下的请求占比；
- `E[retries | block]`：被拦后平均再尝试轮数（含改写、换工具、放弃）；
- `C_round / C_task`：单轮成本 / 单任务总成本（上下文越长，这个比值越接近 1，**放大越接近线性**）。

**只要把这三个量埋进 `agent_trace`，我们就能给出"护栏误拦率 × 重试成本"的自测数字，这比引用任何论文都硬。**

---

## 6. LLM 写 SQL 的错误类型统计（可直接引用的两张表）

### 6.1 BEAVER（275 条错例，含 oracle 子任务标注后仍错的部分）

| 大类 | 占比 | 子类 | 占全部错误 |
|---|---|---|---|
| 分析/结构错误 | **46.8%** | GROUP BY / ORDER BY 漏写或错写、HAVING 位置错（**聚合粒度错**） | **22.8%** |
| | | 窗口/聚合/标量函数用错（PARTITION BY、ROWS BETWEEN、COUNT vs SUM、EXP/LN 丢失） | 14.2% |
| | | 查询分解错（ROLLUP 退化成平铺 GROUP BY） | 7.7% |
| | | 行选择错（多加/漏加 DISTINCT、LIMIT 错、过度去重） | **2.1%** |
| schema linking 错误 | **35.2%** | 表选择错 | 11.8% |
| | | 列选择错 | 12.5% |
| | | JOIN 条件/JOIN 类型错（该 LEFT 写成 INNER，丢行） | **10.9%** |
| 谓词错误 | **18.0%** | 字面量值错（时间/枚举写错） | **7.4%** |
| | | 漏掉必要谓词 | 10.6% |

来源：[BEAVER v3, 2026](https://arxiv.org/abs/2409.02038) §6。

### 6.2 Spider 2.0（300 条错例，企业方言环境）

| 大类 | 占比 | 子类 |
|---|---|---|
| 错误的数据分析 | **35.5%** | 方言函数（DATE_TRUNC、ST_DISTANCE 等）10.3%；高级计算（NTILE、CORR、STDDEV）7.5%；查询规划（多层嵌套/CTE/集合运算）**17.7%** |
| schema linking 错 | **27.6%** | 列 16.6%（Spider 2.0-lite 单库平均 >755 列，BIRD 约 54 列）；表 10.1% |
| JOIN 错误 | **8.3%** | 无显式外键需推断 join key |

来源：[Spider 2.0, 2024](https://arxiv.org/abs/2411.07763) §4.2。

### 6.3 与我们"判分器偏差"清单的映射

| 我们的清单 | 公开数据里的对应 | 量级 |
|---|---|---|
| 聚合粒度错 | GROUP BY/HAVING 位置 | 22.8%（BEAVER） |
| 去重错 | DISTINCT / 行选择 | 2.1%（BEAVER）+ join fan-out 计入 10.9% |
| 时间范围错 | 谓词字面量 + 方言日期函数 | 7.4% + 10.3% |
| JOIN 基数放大 | JOIN 类型/条件错 | 10.9%（BEAVER）/ 8.3%（Spider 2.0） |

**这四条都能在公开文献里找到对应数字，面试时可以逐条对上，不需要自己编。**

---

## 7. 哪些结论能放心引用 / 哪些不能

### 7.1 可以放心说（有一手出处、条件清楚）

| 结论 | 安全表述 |
|---|---|
| 基准饱和 | "Spider 1.0 已经饱和（代码 agent 91.2%），企业基准才刚开始（Spider 2.0 21.3%、BEAVER 10.8%）" |
| 口径的纯效应 | "在 Cube 的 paired 实验里，只加 4KB 口径文档就带来 +17~23pp，且模型间差异被抹平（2026）" |
| 语义层的边界 | "dbt 的数据显示语义层对未覆盖问题是 0%（直接拒绝），自由 SQL 会给错数；补 3 个 model 后覆盖到 100%、准确率 98.2–100%" |
| 错误分布 | "BEAVER 采样的 275 条错例中，结构错 46.8%、schema linking 35.2%、谓词 18.0%" |
| 重试经济学 | "ICLR'24 的自修复研究：等预算下把预算堆在修复上只有 1.05×（甚至 0.97×）；但反馈质量提升带来 1.58×" |
| 判分器噪声 | "BIRD Mini-Dev 标注错误率 52.8%，修正后 16 个 agent 分数变化 −7%~+31%、排名 ±9" |

### 7.2 不能这么说（会被追问翻车）

| 危险表述 | 为什么错 | 正确说法 |
|---|---|---|
| "语义层能把准确率提升 2 倍" | Snowflake 的"2x"是内部 150 题 vs GPT-4o 单 prompt，且是编译器 + 语义模型一起 | "在厂商内部集上，Cortex Analyst 相对单 prompt 约 2 倍（不可跨基准比较）" |
| "语义层提升 20pp"（无条件） | 三个厂商的量级都是 15–25pp，但条件完全不同，且混淆 context/compiler | "口径作为纯 context 的效应是 +17~23pp（Cube）；编译器再叠加多少，**没有干净的公开实验**" |
| "BIRD 的外部知识消融是 +20pp" | 该说法来自 Cube 博客转述（[二手]），我**没有**在 BIRD 原文核对到该数字 | 要么删掉，要么标"转述，未核实" |
| "Spider 1.0 人类 91.2%" | 91.2% 是 Spider 2.0 论文里 agent 的成绩，人类成绩是另一组数字（BIRD 人类 92.96% 才是一手） | "Spider 1.0 上 agent 已到 91.2%（Spider 2.0 论文对照）" |
| "BEAVER 证明口径是主要瓶颈" | BEAVER RQ2 明确：纯结构复杂度题比纯领域知识题**更难** | "BEAVER 显示结构复杂度仍是最大单项，口径/领域知识是第二层" |
| "重试 N 次就能过" | τ-bench pass^8 只有 25%；自修复在等预算下可能负收益 | "重试收益取决于反馈质量，不是次数" |
| "我们的 SQL 准确率 75.8%"（用现在 `judge.py`） | 判分器是关键字重叠，不是执行结果；同一套题另一次跑出 0/25 | "在执行级判分上线前，我们的准确率数字不可对外" |

---

## 7.3 本文引用的全部 URL（附录）

**基准/论文**
- Spider 1.0 — https://arxiv.org/abs/1809.08887
- BIRD — https://arxiv.org/abs/2305.03111
- Spider 2.0 — https://arxiv.org/abs/2411.07763
- BEAVER — https://arxiv.org/abs/2409.02038
- The Death of Schema Linking? — https://arxiv.org/abs/2408.07702
- AmbiQT（歧义题，3000+ 双解） — https://arxiv.org/abs/2310.13659
- SQLStructEval — https://arxiv.org/abs/2604.06736

**语义层实测**
- Snowflake Cortex Analyst 准确率（2024-08） — https://www.snowflake.com/en/blog/engineering/cortex-analyst-text-to-sql-accuracy-bi/
- Snowflake 语义模型自动化（2025-03） — https://www.snowflake.com/en/blog/engineering/agentic-semantic-model-text-to-sql/
- dbt Labs benchmark（2026-04） — https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026
- dbt benchmark 代码 — https://github.com/dbt-labs/dbt-llm-sl-bench
- Cube paired benchmark（2026-04） — https://cube.dev/blog/why-semantic-layers-make-llm-analytics-reliable-a-paired-benchmark-across-three-frontier-models
- Cube 论文 — https://arxiv.org/abs/2604.25149
- 方法学批评 — https://blog.strata.do/p/the-semantic-layer-benchmark-problem
- Databricks 观点（无数字） — https://developers.databricks.com/perspectives/ai-text-to-sql-is-accurate-when-it-is-grounded-and-evaluated

**Agent 行为/错误**
- τ-bench — https://arxiv.org/abs/2406.12045
- Is Self-Repair a Silver Bullet? — https://arxiv.org/abs/2306.09896
- LLMs Cannot Self-Correct Reasoning Yet — https://arxiv.org/abs/2310.01798
- MAST（多 agent 失败分类） — https://arxiv.org/abs/2503.13657
- 标注错误（VLDB 2026） — https://arxiv.org/abs/2601.08778 ｜ 代码 https://github.com/uiuc-kang-lab/text_to_sql_benchmarks
- Anthropic 多 agent 系统（token 放大） — https://www.anthropic.com/engineering/multi-agent-research-system

---

## 8. 对我们仓库的可落地建议（按优先级，≤7 条）

1. **先把判分器换成"执行结果比对"，再谈任何 A/B。**
   改 `backend/tests/agent_evals/judge.py`：当前 `_sql_keyword_overlap(...) >= 0.6`（约 662–700 行）决定 `result.sql_accuracy`，属于"字符串相似"而非"答案正确"。仓库里已有 `_extract_agent_rows` / `_rows_match` / `_normalize_row`（547–660 行）可用于结果集比对，建议改为：**gold SQL 与 agent SQL 各跑一次 → 归一化（列序无关、数值容差、行序无关）→ 比对**，关键字重叠仅作为诊断字段保留。
   依据：BIRD Mini-Dev 标注错误率 52.8%、修正后 agent 分数摆动 −7%~+31%（[uiuc-kang-lab/text_to_sql_benchmarks](https://github.com/uiuc-kang-lab/text_to_sql_benchmarks)）。
   **验收**：同一批 33 题连跑两次，准确率方差 < 2pp。

2. **把"口径探针"做成独立题组，量出"知道口径"的边际收益。**
   在 `backend/tests/agent_evals/dataset.jsonl` 增加一组同义改写题：**同一业务问题 + 两套 gold（口径 A / 口径 B）**，只有引用受治理指标才算对。同时在 `agent_tools.py` 的 `query_datasource` 上加快关（`measures` 只允许 `metric_key`，见 1264/1296 行附近），这样就能跑 **paired A/B：metric_key-only vs field+agg 自由拼** —— 这正是 Cube 的实验设计在我们这里的等价物。
   **验收**：输出 `coverage`（问题被现有指标覆盖的比例）、`EX_covered`、`EX_uncovered`、`Δ = EX_covered − EX_free`。

3. **给护栏加"被拒绝"埋点，这是唯一能回答"行为代价"的办法。**
   `sql_guard_ast.ast_full_check`（482–545 行）已经把 4 条规则分别返回 `(name, label, result)`，把 **命中的 rule 名** 写进 `agent_trace`（现在只返回 reason 文本）；统计 `blocked_rate`、`retries_per_task`、`tokens_per_success`、`p95 latency`，即可算出 §5.3 的放大系数 `R`。
   **验收**：能出一张"每条规则被触发次数 / 其中重试后成功 / 重试后放弃"的表。

4. **修正 `ast_full_check` 的三类误拦（顺序：先降误拦，再谈收紧）。**
   - **窗口/高级聚合**：`check_aggregations`（266–300 行）要求**所有** `exp.AggFunc` 都在 `ALLOWED_AGGREGATIONS`，会把 `PERCENTILE_CONT`、`STDDEV_SAMP`、`APPROX_COUNT_DISTINCT` 等一并拦掉；建议按"窗口上下文（`exp.Window`）内放行 + 聚合函数白名单只约束非窗口聚合"处理。
   - **SELECT ***：`check_select_star`（239–262 行）对 **CTE 内部**的裸 `*` 也拦（注释 226 行明确如此）；建议只在"最终投影"层强约束，CTE 内允许（CTE 会被外层显式列约束）。
   - **CROSS JOIN**：`check_cross_join`（206–214 行）按 `kind/method == 'CROSS'` 拦，对 `a, b WHERE a.id=b.id` 这类隐式连接无效、对显式 `CROSS JOIN ... WHERE` 又误伤；建议改为"**当且仅当不存在连接谓词**时拦"，而不是看关键字。
   **验收**：用 20 条真实业务 SQL（含窗口、CTE、隐式 join）回归，误拦率从当前值降到 < 5%，且不放过真笛卡尔积。

5. **给指标 formula 增加"口径元数据"，让判分器能自动查聚合粒度。**
   `metric_service.assert_formula_allowed`（193–216 行）只校验表达式结构；建议在 `MetricDefinition` 上加 `grain`（聚合粒度，如 day/month）、`time_column`、`additive`（是否可加）三个字段，并在生成/判分两侧校验"问题的时间粒度 = 指标的 grain 或其整数倍"。
   依据：BEAVER 中 GROUP BY/HAVING 粒度错误占 **22.8%**，是单项最大来源。
   **验收**：粒度类错误能在离线评测中被单独计数（而不是混进"SQL 错"）。

6. **把护栏错误信息改成"可执行反馈"，不要只报"不允许"。**
   现状 `return False, "不允许使用 UNION 操作"` —— 这是低质量反馈；自修复研究表明反馈质量决定 **33.3% → 52.6%** 的修复率（1.58×），而单纯增加重试次数是 0.97–1.05×。
   建议 `ast_full_check` 失败时返回结构化对象：`{rule, offending_snippet,为什么, 两条改写建议（示例 SQL 片段）}`，并把 suggestion 直接喂给 repair 提示词。
   **验收**：被拦后的"第二次尝试成功率"提升可测（对照：现状 baseline）。

7. **画我们自己的 coverage–accuracy 曲线，作为对外唯一可引用的数字。**
   公开数据只有 dbt 的 8/11 一个点（[未证实] 存在连续曲线）。做法：对现有 33 题逐题标注"是否可被当前指标集覆盖"，跑 covered / uncovered 两组 EX，再按"新增 1 个指标 → 覆盖 +k 题"做增量实验，得到 `EX = f(coverage)`。
   这是面试里最有说服力的一张图：**它同时回答"口径治理值多少"和"该先建哪些指标"**。

---
