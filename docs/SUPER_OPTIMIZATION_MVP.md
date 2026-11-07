# 超级优化 MVP 清单（feat/super-optimization）

> 决策稿 v1 · 2026-11-07 · 依据本仓三份调研：[市场与商业化](research/01-market-differentiation.md)、[编排链路对标](research/02-orchestration-benchmark.md)、[代码级审计](research/03-codebase-audit.md)。
> 总原则：**不换跑道，只补短板**；每批可独立合并、可独立回滚；**稳定优先于炫技**。
> 命名约定：S<批次>-<序号>，例如 S1-2 = 第 1 批第 2 项。

---

## 0. 一页决策（先看这张表，细节在后面的批次里）

| # | 争议点 | 结论 | 一句话理由（证据） |
|---|---|---|---|
| **D1** | 对话助手与画布助手是否**彻底分家**（两个 Lead 循环） | **不做**。改做「一个循环 + 能力画像表」 | 打分 4.25 / 3.25 / 1.50（共享循环 / 双循环 / 完全独立）。双循环要**复制**确认守卫、记忆回流、评审闸、收尾 done —— 恰好是本仓反复踩坑的四段；且要动 20+ 个后端测试文件 |
| **D2** | 企业级是否引入 **LangGraph** 重写编排 | **不引入编排层**。只做「打开 tracing 旁路 + 借鉴概念」 | 9 个能力点里只有 *args schema 校验*、*tracing 后端* 是框架真增量；断线续收、HITL 三态机、双入口事件翻译框架都不白送。重写 = ~1.5 万行迁移，且与「写工具永不注册」的 6 条测试断言正面冲突 |
| **D3** | 教学版是否用 **LangChain / LangGraph** | **用，但单开 `teaching/` 子项目**，不进主仓回归 | 学生要「框架用法」（简历关键词），也要「判断力」；进主仓会拖依赖、让安全守卫测试变红、让「白盒自研」叙事失真 |
| **D4** | 「工具调用没有日志」怎么补 | **一张 `agent_calls` 表 + trace_id 贯通 + 6 个插入点**；Langfuse 仅作可选旁路 | 默认部署下**没有任何一次工具调用被留存**（Langfuse 默认关、日志只到 stdout、Redis 事件日志 1h TTL 且每轮清空、DB 只存正文）。这是**所有优化能被度量**的前提，必须第一个做 |
| **D5** | 两个助手怎么分化 | **能力画像（数据表）+ 拆 prompt**；对话 = 数据工程/复杂分析/清洗，画布 = 搭建/洞察/呈现 | 现在一份 `LEAD_AGENT_SYSTEM` 服务两个入口，其中约 1/3 篇幅是画布专属规则（KPI 卡片、清空画布、落块顺序）——**已经在泄漏**，只加配置不拆 prompt 等于把泄漏制度化 |

**一句话结论**：先把「看得见」（D4）和「分工明确」（D5）做完，再谈「跑得更聪明」（正确性原语 / 成本闸）；框架与双循环都是**实现手段**，不是能力来源。

---

## 1. 三份调研的结论摘要（不读原文也能决策）

| 文档 | 最该记住的一条 | 落点 |
|---|---|---|
| 01 市场 | 大厂卖「云上按年租」，开源卖「要自己搭的 SQL 生成器」，我们卖**「装进自己电脑、连自己库、带治理和交付物的一人公司 BI」** | 三条硬空隙：「能连生产库且密码不流出去」「指标口径+血缘+定时洞察」「交付物+售后+可审计」 |
| 02 编排 | 生产态数据 Agent 正在收敛为**「确定性管线 + 局部 agentic 决策」**，我们的形态是对的，短板在**正确性原语**不在框架 | 缺 dry-plan、缺 schema 检索裁剪、缺「错误码 → 策略表」 |
| 03 审计 | 可观测性**骨架齐、落地零**；解耦**耦合面比预期小且已分化过半** | 三份画布事件翻译器、四处 LeadAgent 构造、chat 下 `get_canvas_layout` 是死工具 |

---

## 2. 商业化：卖什么、卖多少钱、课怎么排（可直接执行）

### 2.1 三种商品形态

| 形态 | 交付实质 | 建议价 | 区间 | 定价锚点 |
|---|---|---|---|---|
| **A 成品部署包**（引流） | 一键安装脚本 + 离线依赖 + 演示库 + 3 模板 + 上手视频 + 1 年更新 | **699** | 399–1299 | 源码半成品 300–700（互站）；腾讯云 BI 个人版 550/年 |
| **B 代部署 + 陪跑**（主推） | 远程装好 + 接 1–2 个源 + 3 张看板 + 1 次培训 + 7 天答疑 | **2800** | 1500–4999 | 九数云 5888/年起；腾讯云 BI 基础版 7225/年/10 用户 |
| **C 教学版** | 10 章课 + 演示库 + 12 份可交付作业 + 作品集模板（+199 批改） | **299** | 199–499 | 慕课网同类体系课 1199；B 站免费课是地板 |

**明确不卖源码**：源码市场默认预期就是「买完自生自灭」（平台自己写明不对永久更新/永久技术支持担保），卖源码 = 自降身价 + 必然被盗版。交二进制/打包产物 + license（绑机器指纹）。

### 2.2 三条「它做不到而我能」的空隙

1. **能直连你自己的生产库，密码还不流出去**：ChatGPT 数据分析沙箱官方写明不能发起外部网络请求；Power BI Copilot 要 F2+/P1+ 付费容量；Cortex Analyst 要先写 YAML 语义模型；Genie 要先配 Unity Catalog。→ 我们是**开箱直连 + 0 语义建模 + 凭据不出服务器**（`enc:v2` 落库加密、DuckDB `CREATE SECRET`、`env:`/`file:` 凭据引用）。
2. **指标口径 + 血缘 + 版本 + 定时洞察**：大厂绑在 5888–108000 元/年套餐里；我们已有代码（`metric_service` / `metric_lineage` / `insight_engine`）。
3. **交付物 + 售后 + 可审计**：开源 Agent 与源码贩子都不提供；我们卖的是「装得上 + 跑得起来 + 有人回话 + 出事能查」（后者正是第 S1 批要补的）。

### 2.3 课程（10 必修 + 2 选修）

1 全流程与环境 · 2 数据接入与数据安全感 · **3 数据体检** · **4 数据清洗（干跑）** · 5 描述统计与选图 · **6 指标口径/版本/血缘** · **7 对话式分析：怎么问、怎么验 AI** · 8 洞察与定时报告 · 9 看板与交付 · 10 结课经营闭环 · 11（选）TLS/只读账号/轮换 · 12（选）把作业变成简历。

**差异化不靠内容，靠「环境 + 验收」**：每人一套能跑的环境 + 12 份可交付作业 + 每章埋一个「AI 会说错」的场景教核验。第 6、7 章是独家内容（企业第一周就要用，免费课不教），适合先切片引流。

---

## 3. 超级优化 MVP 清单（5 个批次，约 3 周）

> 每批 = 一个独立 commit + 一个独立开关 + 一次全量回归。**任何一批出问题，关开关即可回到当前行为。**

### Batch 0 · 零行为清理（0.5–1 天，可直接合 master）

| 编号 | 问题（证据） | 改法 | 验收 |
|---|---|---|---|
| **S0-1** | 工具结果被**截两次**：`lead_agent_unified.py:425-427` 的 `[:4000]` + `compress` 的 800 token 预算，违反 `compress.py:1-10` 自述「所有截断只允许出现在这里」 | 删掉循环里的 `[:4000]`，只留压缩层一处 | 同一结果在不同入口长度一致；`test_lead_p4_tool_result_compression.py` 绿 |
| **S0-2** | 观察类工具**串行 await**（`lead_agent_unified.py:307`），模型并行发 3 个观察实际仍是 3 次往返 | 按 `tool_class == observe` 分组 `asyncio.gather` | 3 个观察的 step 耗时 ≤ 最慢一个；事件顺序仍按原顺序 emit |
| **S0-3** | ledger 只在**循环结束**落 trace（`:537-543`），看不到「第几步开始超预算」 | 移到每步末尾（循环内） | trace 里每步一份 ledger；总账仍保留 |
| **S0-4** | LLM 一次 5xx 就把整轮推进「确定性收尾」（`:271-280`），无重试、不区分错误类型 | `tool_call_round` 外层加 1 次退避重试，仅对上游 5xx/超时；JSON 非法不重试 | 上游抖动不打穿整轮；新增 `test_llm_retry_classification.py` |
| **S0-5** | `degradation` 是 `list[str]`，「配额打满 3 次」与「偶发 1 次」在事件里长得一样 | 改成计数（`dict[str,int]`），done 事件保留原字段 + 新增 `degradation_counts`（向下兼容） | 前端不改也不炸；能按次数排序降级原因 |

### Batch 1 · 可观测底座：**工具调用日志**（3–4 天）← 用户点名要补的那件事

> 目标：一轮对话从「只有一个最终答案」变成「一条可按 trace_id 排序的完整时间线」。

| 编号 | 内容 | 落点 | 改动量 |
|---|---|---|---|
| **S1-1** | `trace_id` 贯通：入口生成 → LeadContext → 执行器/记录器 | `ai.py:667`、`ai.py:1723` 各加一行；`ToolExecutor.__init__` 已是 keyword-only，加 3 个默认参数即可，**调用点零改动** | S |
| **S1-2** | `agent_calls` 表（迁移 `0023_add_agent_calls`，当前 head `0022`）：`trace_id/seq/kind/entry/mode/parent/session_id/message_id/user_id/tool_name/tool_call_id/args_json/args_hash/ok/error_kind/error_layer/error_msg/result_chars/result_preview/duration_ms/attempt/memo_hit/model/tokens_in/out/cached` + 3 个索引 | 新表 + 新文件 `services/agent_trace.py`（约 120 行，`record()` 同步零 await、队列满即丢弃计数、单写入者批量 flush，复用 `chat_task._log_writer` 范式） | S/M |
| **S1-3** | 六个插入点：① `ToolExecutor.execute_tool_call`（唯一 choke point，覆盖 95%）② 白名单拒绝分支（越权尝试目前完全静默）③ `run_analysis` 收尾（**已有 `tool_chain` 在内存里白白丢弃**）④ `lead_observe.recon_tool`（观察工具绕过 registry，完全不进日志）⑤ LLM usage 5 处 ⑥ 两个任务收尾写 turn 行 + flush | 见 `03-codebase-audit.md` §A.3.2 表 | M |
| **S1-4** | 脱敏：SQL 字面量用已有 `sqlglot` 把 Literal 换成 `?`；命中敏感列 → `args_json` 整体置 `[omitted]`；**连接串/密码永不记录**（只放行白名单键）；`args_json` ≤2KB、preview ≤500 字 | `sensitive_columns.py` + `credential_refs.py` 的既有约定，不新写策略 | S |
| **S1-5** | SSE 的 `tool_call`/`tool_result` 补 `tool_call_id`（数据已在 `tc` 里），前端旧逻辑保留 → 精确配对 | `tool_executor.py:162,189` | S |
| **S1-6** | 一条命令看时间线：`scripts/agent_trace.py --trace <id>` 打印表格；错误码枚举（`guard_l1/l2/l3`、`ast_rule`、`sensitive_column`、`query_timeout`、`whitelist`、`bad_args`、`tool_exception`、`llm_timeout`…）**全部从现有代码派生，不新增业务逻辑** | 新脚本 + `error_kind` 映射函数 | S |

**验收（可判定）**：8 个运维问题里至少 7 个能回答 —— 这轮为什么没出图 / 慢在哪 / 是不是在反复重试 / 被哪层安全策略拦了 / 一轮花了多少钱 / 哪个工具最易失败 / 哪个入口更差 / memo 命中率。新增 `test_agent_calls_log.py`（三件事：三条编排路径都产生 tool 行、白名单拒绝产生 `error_kind=whitelist`、`record()` 队列满不抛且主链路 <1ms）。

### Batch 2 · 能力画像与双助手分化（4–5 天）← 用户说的「对话助手应该有清洗/处理能力」

| 编号 | 内容 | 落点 | 改动量 |
|---|---|---|---|
| **S2-1** | 新增 `agents/lead/capability.py`：`EntryProfile` 数据表（prompt 片段 / 工具子集 / 上下文源 / 配额 / 终止条件 / 派发内核 / 产物白名单）。**禁止 `if entry == "canvas"`** | 新文件；沿用 `lead_worker.py` 已验证的「表驱动」范式 | S |
| **S2-2** | 拆 prompt：`LEAD_CORE`（角色/权限/终止/表达）+ `CHAT_PROFILE`（分析步骤/口径优先/数字后验/清洗流程）+ `CANVAS_PROFILE`（报告骨架/KPI 卡片/布局/破坏性确认） | `ai_prompts.py:707-778`；保留 `_sync_prompt_fallback.py` 与字节一致测试 | M |
| **S2-3** | `lead_tool_specs(profile)` + 配额按 profile；**chat 去掉 `get_canvas_layout`**（死工具，`canvas_state.py:69-71` 返回空串） | `lead_agent_tools.py:89-100,136`；`QUOTA` 改为 profile 字段 | S |
| **S2-4** | 上下文源按 profile 选：`canvas layout` 只进 canvas；chat 增「数据质量摘要」源（接体检结果） | `context/provider.py:53-140` | S/M |
| **S2-5** | `dispatch_analysis` 增 `constraints`（数据源/时间范围/输出形态）—— `lead_dispatch` 已有该参数，直接透传 | `lead_agent_tools.py:103-114` | S |
| **S2-6** | 三份画布事件翻译器 → 合并两份（删 `ai.py:1970-2164` 的内联版，与 `canvas_task.py:73-156` 合流） | `ai.py` / `canvas_task.py` | M |
| **S2-7** | 单测锁 profile 差异：两 profile 的工具集差集 == 预期集合；prompt 不允许出现对方入口专属词 | 新 `test_entry_profiles.py` | S |

**验收**：chat 入口工具表里 `get_canvas_layout` 消失；canvas 的 KPI/落块规则不再出现在 chat 的 system 里（按词表断言）；数据体检（`DATA_QUALITY_CLEANING_PLAN.md` M1）作为 chat profile 的**第一个新能力**接进来验证画像可扩展。

### Batch 3 · 成本与正确性闸（4–5 天）

| 编号 | 内容 | 落点 |
|---|---|---|
| **S3-1** | `LEAD_AGENT_MAX_LLM_CALLS` 总预算闸（子内核已有 24，Lead 层现在**无上限**：最坏 6 步 ×(1+24)） | `lead_agent_unified.py:255` |
| **S3-2** | 首步流式（现在整步 `streaming=False`，TTFT = 整步生成时间；legacy 的 answer 分支反而是流式） | `:263-265` |
| **S3-3** | 错误码 → **策略表** `agents/repair_policy.py`：`query_timeout`→缩时间窗重试、`bad_column`→重新取 schema 后改写、`guard_l3`→提示用户换只读账号、`sensitive_column`→请求授权……（**这是「SQL 自修复」的前置条件**，没有码表，自修复只能是 prompt 劝告） | 新文件 + `tool_executor` 接入 |
| **S3-4** | dry-plan 最小版：生成前校验「表/列是否存在、聚合与维度是否冲突、时间范围是否越界」，失败直接回喂而不是等执行报错 | `query_engine.py` / `lead_tools` |
| **S3-5** | `complexity` → 预算档位（simple→react 6 轮，complex→orchestrator 24），让「这次别太贵」可表达 | `lead_agent_tools.py:103-114` + 内核 |

### Batch 4 · 教学与商业化（可与 2/3 并行，不进主链路）

| 编号 | 内容 | 备注 |
|---|---|---|
| **S4-1** | `teaching/langgraph-demo/`：**同一业务场景**（取数→出图）三实现对照 —— `01_raw_sdk.py`（= 本仓思路）/ `02_langchain_agent.py` / `03_langgraph.py`（含 checkpointer + interrupt）。独立 `requirements.txt` + 独立 pytest，CI 不纳入主仓 | 课程作业：diff 三版的「工具注册/循环/中断/观测」，写「框架替我做了什么、代价是什么」 |
| **S4-2** | 闲鱼 A 包交付物：一键安装脚本（Windows 优先、离线依赖、失败回滚）+ 演示库（3 套行业样例，开箱出图）+ 3 模板 + 上手视频 + 已知问题清单 | 已知问题**主动写**，能挡掉一半差评 |
| **S4-3** | 课程 10 章讲义骨架（对应 §2.3 大纲）+ 12 份作业说明与评分标准 + 作品集模板 | 学生版材料带用户 ID 水印溯源 |
| **S4-4** | 部署包 license（机器指纹 + 有效期 + 功能开关）+ 更新通道鉴权 + 试用镜像（限时/功能受限） | 对策：分阶段发货，把「描述不符」这条退款理由用试用期提前消化 |

---

## 4. 顺序与依赖

```
Batch 0（零行为）──┬─→ Batch 1（可观测）──┬─→ Batch 2（画像/分工）──┬─→ Batch 3（成本/正确性）
                  │                     │                        │
                  └─────────────────────┴────────────────────────┴─→ Batch 4（教学/商业化，并行）
```

**为什么是这个顺序**：
1. **先看得见**（B1）：不看日志就改循环，等于关灯修车；且 B2/B3 的效果**必须**用 `agent_calls` 度量。
2. **再分好工**（B2）：用户最直接的诉求是「两个助手别重叠」，且这两个真问题（死工具、prompt 不分入口）成本最低。
3. **最后调优**（B3）：S3-3 依赖 S1 的错误码，S3-4 依赖 S2 的 constraints，硬排在后面是对的。
4. **商业化不等代码**（B4）：课程与部署包是文案+脚本工作，可并行。

**中途切分支的信号**（不满足就别拆）：只有当 `agent_calls` 数据显示「两入口的决策路径确实差异到必须分家」时，才做 D1 里被否掉的「双循环」；现在**没有这个证据**。

---

## 5. 每批必须过的门（合并条件）

1. 全量 `pytest` 通过，**基线 1136 passed**（当前快照，改前先重跑确认）；
2. 前端 `tsc --noEmit` + `npm run build` 干净；
3. 本批新增测试文件存在且能独立跑；
4. 有回滚开关：B0/B1 `AGENT_CALL_LOG=off|full`；B2 `LEAD_PROFILE_ENABLED`；B3 `LEAD_AGENT_MAX_LLM_CALLS`（设 0 = 不限）；
5. 架构不变量测试全绿：`test_architecture_consistency.py`（写工具永不注册、ENTRY_WORKERS 引用有效）、`test_lead_p1_agent_tools.py`（工具表 ∩ 写工具 = ∅）。**这五条不变量一条都不许为优化让步。**

---

## 6. 明确「不做」清单（写下来防止反复讨论）

| 不做 | 理由 |
|---|---|
| 用 LangGraph 重写编排层 | 与 5 条不变量冲突；事件协议/checkpointer/ToolRegistry 三个不可逆点 |
| 两个独立 Lead 循环（完全分家） | 复制四段高价值逻辑；20+ 测试文件；无数据支撑 |
| 在 Lead 循环里塞沙箱代码执行 | 沙箱的边际收益主要落在「清洗/特征工程」，那是**执行层**的事；清洗已有独立计划（派生数据源 + 9 算子 + 干跑） |
| 卖源码 | 必然被盗版 + 自降身价；卖二进制 + license |
| 把 langchain 装进主仓 | 依赖变重、守卫测试失效、白盒叙事失真 |
| 现在做 C（管理员预置连接/RBAC 共享）、D（云 IAM/OAuth） | 已与用户确认暂缓，不是本轮范围 |

---

## 7. 改动前必须先采的 5 个基线数字

> 没有基线就没有「优化」。这些数字现在大多**查不到**——这正是 Batch 1 要做的事。

| # | 指标 | 现在能不能测 | 采法 |
|---|---|---|---|
| 1 | 一轮对话的 LLM 调用次数 / 时长 / token | ❌（只有无关联 stdout 行） | B1 后按 trace_id 聚合 |
| 2 | 工具调用成功率 / 失败类型分布 | ❌ | B1 后按 `tool_name` + `error_kind` 聚合 |
| 3 | 首字延迟 TTFT | ⚠️ 需手工掐表 | B1 记录 turn 行时间戳 |
| 4 | 20–50 题评测集通过率 | ✅ `tests/agent_evals/` 已有 | 改前跑一次存档 |
| 5 | 单轮成本（元） | ❌（token 只进日志） | B1 后 `tokens_in/out/cached` 求和 |

**建议在第一周内补一次「30–50 题双跑对拍」**（single_agent vs legacy），因为代码注释里那个「+32% 调用、63% 走 react」只是 20 题小样本，所有成本判断目前都建立在它上面。

---

## 8. 分支与提交约定

```
分支：feat/super-optimization（从 master 起）
提交：每批一个 commit，subject 前缀 [S0]/[S1]/[S2]/[S3]/[S4]
合并：单批 → master 可快进（每批都能独立回滚）；B2 建议先在分支上跑满一周再合
迁移：0023_add_agent_calls（B1）
```

**已知工作区状态**（本分支起点）：`master @ a4aa444`，另有 5 个未提交改动（`chat_task.py` / `event_stream.py` / `worker.py` / `run_agent_evals` / `ChartRenderer.tsx`）与若干 `_probe_*.py` 探针脚本——**不属于本清单范围**，合并前需单独确认归属。

**注意**：本仓 `.gitignore:176` 忽略 `docs/`，所以 `docs/research/*.md` 与本文件默认不进版本库。若要随分支入库：`git add -f docs/SUPER_OPTIMIZATION_MVP.md docs/research/`。

---

## 9. 三句话给未来（面试/路演可直接用）

1. **分工**：「一个决策循环 + 一张能力画像表」——新增助手只加一行配置，不加一个循环；反对用第二个循环解决分工问题。
2. **可观测**：「先让每一次工具调用都能按 trace_id 排序」，再谈优化；没有时间线的 agent 就是在关灯修车。
3. **框架观**：LangGraph 是**实现手段**不是**能力来源**——真正决定数据 Agent 好不好用的是正确性原语（干跑校验、错误码策略、数字后验）与上下文预算。
