# 05 · 感知与上下文：产品级架构复审

> 评审立场：**这套架构作为"能卖给企业的商品"，够不够格。** 默认假设它太轻，用证据证明或证伪。
> 评审范围：轴 1 感知（perception）、轴 2 上下文（context）。代码不在此轮修改。
> 证据口径：所有断言带 `file:line`；无法从代码确认的标「未确认」。
> 实测数字来自仓库自身的 `estimate_tokens`（`backend/app/services/context_utils.py:33-51`）+ 真实 `TurnContextProvider.for_agent_loop`（`provider.py:115-144` / `assembler.py:266-350`），
> 用合成输入跑出来（方法见 §4）；单文件回归 `tests/test_context_assembly.py` 10 passed 验证骨架未被误读。

---

## §0 一页判决

| 轴 | 判决 | 一句话 |
|---|---|---|
| **感知** | **太轻** | 模型能"看"，但看到的是**回执的一句话**和**上限 900 字/1000 字/2 条的摘要**；它看不见子任务里真正发生了什么，也看不见数据长什么样。 |
| **上下文** | **偏轻** | 分层（Source/Compressor/Assembler/Provider）和台账是真做了工程；但**预算 enforced=False、无窗口对齐、无超窗降级、前缀每步失效**——骨架好，阀门没装。 |

**感知 · 3 条最致命缺口**
1. **子任务回执退化成一句话**：派发后回给模型的 `role=tool` 只有 `review_note`（`lead_agent_unified.py:543`），真正的 `build_execution_summary`（工具链/规划/数值校验/报告 1500 字，`lead_review.py:73-123`）只进 `turn_summaries`，而后者在上下文里**只保留最近 2 条、每条截断 1000 字**（`sources/state.py:13-15,48-49`）→ 第 3 次派发后"第一次做了什么"彻底从模型视野消失。这就是"摘要的摘要"。
2. **数据形态盲区**：全链路只有 `query_sample` 5 行 × 最多 12 列 × 单值 80 字（`lead_observe.py:83-85,129-133`）和字段名+类型。**没有行数、基数、空值率、时间范围**（`lead_tools.py:401-409` 的 datasource 字典里连 row_count 都没有）→ "选对维度和聚合"靠猜。
3. **没有自己的动作账本**：模型不知道"我现在第几步/上限 6 步"（`LEAD_AGENT_MAX_STEPS=6` 只在代码里，`lead_agent_unified.py:158`；上下文里没有任何步数/预算提示），也没有 todo/plan 状态对象（对比 Claude Code 的 todo、Manus 的 `todo.md`）。多步分析里它只能靠 2 条摘要回忆自己刚做了什么。

**上下文 · 3 条最致命缺口**
1. **装配结果与模型窗口之间没有任何对齐代码**：全仓库搜不到 `context_length / max_input / window` 之类的守卫；超窗时上游 400 → `AIUpstreamError`（`llm_client.py:241-244`）→ 被 `lead_agent_unified.py:287-296` 当成"LLM 不可用"→ 用户看到 **"服务暂时不可用，请稍后重试。"**，整轮丢光，且**没有任何压缩/重试**。这是"客户当场炸"的第一现场。
2. **两条无界通道**：`metrics_ctx` 全量注入 system，无条数上限（`ai.py:931-947,979` → `metric_service.py:247-277` 无 cap），实测 300 条指标 = **6348 token/步**；对话窗口 `compressible=False`（`sources/window.py:67`）且 `for_agent_loop` 原样直发（`assembler.py:343`），实测 8 轮报告型历史 = **8126 token/步**。两者都不受 `budgets.py` 管。
3. **system 中段每步被改写 → 前缀缓存几乎全废**：`refresh_canvas` 换掉 CANVAS 块（`provider.py:95-97`），CANVAS 排在 METRICS/WINDOW 之前（`provider.py:73-80`），实测可缓存前缀**只有 2923 / 9470 token（系统段 31%）**，其后 6.5k 恒定内容每步全价重发；重载场景整轮输入 **≈10.6 万 token**（17.7k × 6 步）。

---

# 轴 1 · 感知（perception）

## 1.1 现状：Agent 到底"看得见"什么

### 上下文里注入了什么（唯一入口 `provider.for_agent_loop` → `assembler.for_agent_loop`）

| 块 | 渲染 | 上限 | 代码 |
|---|---|---|---|
| SYSTEM | `LEAD_AGENT_SYSTEM`（实测 2849 字 / **1899 token**） | 无（`enforced=False`） | `ai_prompts.py:602`、`assembler.py:325-338` |
| 历史记忆 | 四节摘要全文 | 只有"生成时 ≤600 字"（prompt 约束） | `sources/memory.py:14-21`、`lead_agent.py:27-50` |
| 窗口 | 最近 30 条**原文**（`LEAD_MAX_TURNS_IN_CTX=30`） | **无**（`compressible=False`） | `provider.py:67`、`sources/window.py:60-68`、`assembler.py:328-330` |
| 数据源 | `- id=… name=… type=…` **只有三字段** | 无 | `sources/state.py:20-27` |
| 画布 | 布局文本（`render_canvas_layout`） | **900 字** | `canvas_tools.py:299`、`canvas_state.py:74-83` |
| 指标 | 全量指标清单 | **无** | `sources/state.py:92-106`、`metric_service.py:265-277` |
| 子任务摘要 | 最近 **2** 条，每条 **1000** 字 | 硬编码 | `sources/state.py:13-15,42-49` |
| 子任务评估 | 最近 1 条 `[子任务评估]` 文本 | 无（但与上条重复注入） | `sources/state.py:36-39` |
| 工具结果 | 当轮 `assistant.tool_calls` + `role=tool` | **800 token/条**（唯一真正生效的上限） | `assembler.py:299-312`、`compress.py:151-166` |

### 逐项：看得见 / 看不见

| 感知对象 | 现状 | 看得见的证据 | 看不见的部分 |
|---|---|---|---|
| **用户意图** | 有 intent 标签，但**只在 SSE 事件里**，没有回写进 messages | `lead_agent_unified.py:329-362` | 模型看不到"我判定本轮是 analysis"，也无法在后续步骤复核 |
| **画布状态** | canvas 入口每步可见（900 字）；派发后 force 重读 | `canvas_state.py:69-71,74-83`、`lead_agent_unified.py:505-510` | chat 入口**恒为占位句**"（非画布入口或画布状态不可用）"（`sources/state.py:30-33`）；**用户中途手改画布不会刷新**（refresh 只在派发后调用）；块内数据（series/配置）完全不可见，只有标题+类型+坐标 |
| **数据源 schema** | 系统块只有 id/name/type；字段名走 user 消息的散文注入 | `state.py:20-27`、`ai.py:918-927` | `description`/`table_ref`/`fields` 在 `lead_tools.py:401-409` 里都有，**被 render 丢掉了**；未选中的数据源永远没有字段 |
| **字段基数/分布/时间范围** | 无 | — | **全缺**：没有行数、distinct、null 率、min/max 时间、枚举值域 |
| **子任务进度** | 有（`StepProgress` → `progress` 事件） | `lead_perception.py:74-167`、`lead_tools.py:905-907`、`lead_dispatch.py:131-143` | **只发给前端，不回灌模型**；模型在派发期间是"黑箱等待" |
| **失败原因** | 评估文本里最多 3 个失败工具名 | `lead_tools.py:173-175` | `result.error` 全文只在 exec_summary 里截 200 字（`lead_review.py:117`），而该摘要在第 3 次派发后已被挤出（见 1.3） |
| **自己的历史动作** | 2 条 turn_summaries + 900 字画布 | `sources/state.py:13-15` | **无动作账本/步号/已派发 goal 列表**（`seen_goals` 是纯代码变量，`lead_agent_unified.py:249`，模型看不见） |
| **时间/成本** | 无 | — | `elapsed_ms` 只在 SSE 的 tool_result 里（`lead_dispatch.py:200-211`）；`Ledger` 只进日志/trace（`lead_agent_unified.py:268-274`），**never 进 messages** |

## 1.2 对标：哪些"通用能力"对数据产品是必需品

| 对标物 | 它的感知机制 | 对我们的必要性 | 理由（落到我们的文件） |
|---|---|---|---|
| **Claude Code** | ① todo 列表（显式任务状态）② 文件系统即工作区（读前先看）③ 上下文压缩 + 计划外置 | ① **必需** ② 部分等价（画布=文件系统）③ 必需 | 我们没有 todo：模型靠 2 条摘要回忆；"读前先看"我们只有 5 行样本 |
| **Manus** | 虚拟机 + 文件系统做外部记忆；append-only、前缀稳定 | **必需（精神）** | 我们的"外部记忆"是画布（已有），但**中间产物（SQL/结果集/校验结论）用完即弃**，连 `_extract_quantified_numbers` 都在补漏（`lead_agent.py:128-145` 注释自认） |
| **Devin** | browser + screenshot 感知真实 UI | **不需要** | 我们不是操作别人 UI 的产品；等价物是"数据画像"（见 1.4） |
| **LangGraph supervisor / OpenAI Agents SDK** | subagent 回传**结构化结果**给 supervisor，supervisor 在 state 里读 | **必需（我们正好缺）** | 我们**有** `RunAnalysisResult`（success/report/steps/tool_chain/verified，`lead_tools.py:45-58`），却只把一句话交给模型（`lead_agent_unified.py:543`）——**有货不给**，这是最贵的一种缺失 |

**结论**：对数据产品，必需品是 ①子任务结构化回执 ②动作账本 ③数据画像；可选是 ④工件外部记忆；不需要是 ⑤浏览器/截图。

## 1.3 关键质疑：`turn_summaries` 是不是"摘要的摘要"

**证据链（全部可核）**

1. 一次派发会往 `ctx.turn_summaries` 追加**两条**：
   - `build_execution_summary(...)`（`lead_dispatch.py:237`，仅 `result.success` 时；失败时**连这条都没有**，`lead_dispatch.py:230`）
   - 评审结论 `outcome.text`（`lead_agent_unified.py:531`）
   - 额外：失败步骤还会追加 `render_progress_text(p)`（`lead_dispatch.py:148-149`）
2. 注入时只取**最近 2 条**（`SUBTASK_KEEP=2`，`sources/state.py:14`），每条截断 **1000 字**（`SUBTASK_SUMMARY_CHARS=1000`，`sources/state.py:13,49`）。
3. 因此第 2 次派发后，模型看到的 `【已完成子任务摘要】`= `[exec2, review2]`；**第 1 次的 exec_summary 永久消失**（它在 tool 消息里也从未出现过——工具回执只有 `review_note`，`lead_agent_unified.py:518-543`）。
4. `build_execution_summary` 自己还截过一轮：steps 前 12 条、tool_chain 前 12 条、画布 400 字、报告 1500 字（`lead_review.py:97,102,115,121`）。**报告 1500 字再被 1000 字砍一刀，且砍在报告中部**——证据（数字）恰好是最容易被砍掉的部分。
5. 同一段文本被注入**两遍**：`[子任务评估]` 既在 SUBTASK 块（若它属于最近 2 条）又在 ASSESSMENT 块（`state.py:36-39` 单独取最后一条评估）。实测重复约 259 token/步。

**推演（多步分析现场）**：`LEAD_AGENT_MAX_STEPS=6`（`config.py:232`）、dispatch 配额 3（`lead_agent_tools.py:49`）。
一个"先查华东、再拆渠道、再同比"的三段分析：

| 步 | 模型能看到的子任务信息 | 它"忘了"什么 |
|---|---|---|
| step2（第 1 次派发后） | exec1 + review1（≤1000 字/条） | — |
| step3（第 2 次派发后） | exec2 + review2 | **exec1 消失**：第一次用了哪个表、哪个口径、哪些数字已验证，全没了（只剩画布里 900 字的图表标题） |
| step4（第 3 次派发后） | exec3 + review3 | 前两次全没了；此时若评审 fail，`review_note` 里那句"小缺口用 repair 补"没有了上下文，模型会**重做整段分析**或**换一个方向重派** |

**结论**：会。而且是**结构性**会，不是偶发。触发条件：**同一轮内派发 ≥2 次**（即绝大多数多步分析）。表现出来是"重复派发同一目标""前后两次用了不同口径""问它刚才查了什么答不上来"。
**注意这不是"模型笨"**：`already_dispatched` 的去重逻辑在代码里（`lead_agent_unified.py:473-474`），但模型看不见 `seen_goals`，它只能靠摘要判断，而摘要已被截断。

**改法**
- **S**：`lead_agent_unified.py:543` 把 `review_note` 换成 `exec_summary + review_note`（exec_summary 已算好，就在上一行 `523`，零额外成本，只是原来被丢掉了）。收益：本次派发的完整信息进入**工具结果**（append-only，不会被 keep=2 挤掉）。
- **S**：`sources/state.py:13-15` 的 `SUBTASK_KEEP/SUBTASK_SUMMARY_CHARS` 从硬编码常量改为"按剩余预算取尽量多"，或直接**删掉 SUBTASK 块**（因为 ① 已在工具结果里 ② 画布块承担现状）。
- **M**：新增"动作账本"块：`[{step, tool, goal, verdict, key_numbers}]` 结构化列表，由代码维护、每步追加重放（落点：`lead_agent_unified.py:546` 的 `add_tool_result` 旁边；数据源：现有 `RunAnalysisResult` + `seen_goals`）。

**不做的事故场景**：客户说"再补一张同比图"，模型重派一次完整分析（`dispatch_analysis`），画布上出现**重复的 4 张图**，且第二次的 GMV 与第一次差 0.3%（口径不同：含税/不含税）。客户当场问"为什么两个数不一样"，模型答不上来——因为它看不到第一次的口径。

## 1.4 数据产品的特殊感知：`query_sample`(5 行) + `list_fields`(名+类型) 够不够

**不够。** 具体缺的是"决定聚合方式"的三个量：

| 需要的感知 | 为什么决定成败 | 现在有没有 |
|---|---|---|
| 行数 / 量级 | 决定"能不能 GROUP BY 后直接画" vs "要不要先 top-N + 其他"；决定"1.2 万行"这类叙述真假 | 无（`lead_tools.py:401-409` 连 row_count 都不放） |
| 维度基数（distinct） | 省份 31 个可画饼图；order_id 8 万唯一值画饼图 = 灾难 | 无 |
| 空值率 / 时间范围 | 决定 `WHERE` 要不要兜 NULL、时间过滤写 2024 还是 2024Q1 | 无（只有 5 行样本里能"碰巧"看到一个日期） |

5 行样本能覆盖的只是"字段名长什么样、值是中文还是英文、日期带不带时分秒"——这是**形态**，不是**分布**。

**最小补齐（S，1 个工具，1 次查询）**：新增 `profile_datasource` 观察工具，一条 SQL 出画像：

```sql
SELECT count(*) AS rows,
       count(DISTINCT "province") AS province_n, count(*) FILTER (WHERE "province" IS NULL) AS province_null,
       min("order_date") AS min_d, max("order_date") AS max_d
FROM <table_ref>
```

- 落点：`lead_observe.py` 新增函数 + `recon_tool` 分支（`lead_observe.py:232-250`）+ `lead_agent_tools.py:89-100` 注册 + `OBSERVE_TOOL_NAMES`（`lead_agent_tools.py:33-39`）。
- 约束沿用现有铁律：显式列名（`sample_sql` 同款，`lead_observe.py:88-91`）、走 `QueryDatasourceTool`（guard/归属/超时一条链，`lead_observe.py:143-151`）、失败即降级。
- 成本：1 次查询（DuckDB 对已物化表是毫秒级）+ 输出 ≤300 token。
- **必须做成"一次拿全所有列"**，而不是每列一次（否则 12 列 = 12 次工具调用，配额 2 直接卡死，见 `QUOTA` `lead_agent_tools.py:49`）。

**不做的事故场景**：客户的数据源里 `province` 有 8 个变体（"浙江"/"浙江省"/"ZJ"/"浙江省 "/…），模型只看到 5 行里的"浙江"，`GROUP BY province` 出 8 行，饼图碎成 8 片。客户："这不是我们的 31 个省。"——一次诊断就把产品判死。

## 1.5 轴 1 差距汇总与改法

| # | 差距 | 改法 | 规模 | 落点 |
|---|---|---|---|---|
| P1 | 工具回执只有一句话 | 回执 = `exec_summary`（已算好）+ `review_note` | **S** | `lead_agent_unified.py:543`（exec_summary 在 :523 已有） |
| P2 | 子任务摘要 keep=2 / 1000 字 | 删 SUBTASK 块或改预算驱动；加动作账本 | S/M | `sources/state.py:13-15`、新块 `assembler.py:130-138` |
| P3 | 数据分布盲区 | `profile_datasource`（一条 SQL：行数/基数/空值/时间范围） | S | `lead_observe.py`、`lead_agent_tools.py:89-100` |
| P4 | 数据源块丢字段/描述 | `render_datasources` 补 `description`+`row_count`，字段名放系统块而非 user 散文 | S | `sources/state.py:20-27`、`lead_tools.py:401-409` |
| P5 | 无步号/预算感知 | system 末尾加一行 `[本轮进度] step 2/6；observe 1/2；dispatch 1/3` | S | `lead_agent_unified.py:258` 前拼进 `system_text` |
| P6 | 派发期间黑箱 | 把 `StepProgress` 的 fail 行也写入下一条 tool 回执（现只写 turn_summaries） | S | `lead_dispatch.py:148-149` → `lead_agent_unified.py:543` |
| P7 | 时间/成本不可见 | 回执里带 `elapsed_ms` / `input_tokens`（`result.elapsed_ms` 已有） | S | `lead_dispatch.py:200-211` 同源数据 |

---

# 轴 2 · 上下文（context）

## 2.1 逐项核：`enforced=False` 到底意味着什么

**代码事实**

| 事实 | 位置 |
|---|---|
| 四张表全部 `enforced=False` | `budgets.py:60`（dataclass 默认）、`:135`（agent_loop）、`:154,169,185`（TARGET 表） |
| `ACTIVE` 指向**等价表**而非 TARGET 表 | `budgets.py:190-196` |
| `for_decision` / `for_answer` / `for_agent_loop` 三处裁剪都被 `if table.enforced:` 包住 | `assembler.py:198,224,316` |
| 唯一真正生效的上限：工具结果 800 token/条 | `assembler.py:299-312`（注释明说"与 enforced 开关无关"） |
| 台账算完只 `logger.info` + 可选 trace span | `lead_agent_unified.py:268-274,557-565` |
| `Ledger.over_budget` 存在但只用于日志字符串 | `types.py:118-120,149` |

**意味着什么（不是理论，是三条真实后果）**

1. **没有任何代码知道"这次装配是否超窗"**。全仓库没有模型窗口常量，也没有 `prompt_tokens > N` 的检查（搜索证据：只有 `sql_guard.MAX_INPUT_LENGTH=2000`，与 LLM 无关，`sql_guard.py:30`）。
2. **超窗 = 整轮丢失**。上游返回 400 → `AIUpstreamError`（`llm_client.py:241-244`；非流式在 `:334-336`）→ `stream_unified` 的 `except`（`lead_agent_unified.py:287-296`）把任何 LLM 异常都归为"服务暂时不可用"。**没有 compaction、没有重试、没有降级到小上下文**。用户看到一句话，画布没动，本轮所有事件作废。
3. **预算表成了"装饰性文档"**：TARGET 表写着"决策输入 ~4050 token"（`budgets.py:155`）"Worker 输入 ~7950"（`:186`），但实际路径一条都不按它裁。**危险点在于读者会以为有阀门**——这正是"产品级"和"能跑"的分界：前者保证最坏情况有上界。

**`MESSAGE_LIST_MAX_TOKENS=90000` 与模型窗口的关系**

| 问题 | 事实 |
|---|---|
| 它是什么 | `config.py:246`，注释说"上下文压缩（消息列表）" |
| 它在哪生效 | ① `compress_history` 的默认 `max_chars`（`context_utils.py:119-121`）② `MessageListPolicy` 兜底（`compress.py:97`）③ `equivalent_worker_table` 的 window 预算（`budgets.py:104,109`） |
| **它是上限吗** | **不是。** `compress_history`（`context_utils.py:111-137`）只做两件事：保留 system + 最近 `keep` 条、插一条"已省略 N 条"的 digest。**它从不按 `max_chars` 裁剪保留内容**——折叠后尾部超限也照样返回。所以 90000 只是"触发折叠的门槛"，不是天花板 |
| 与窗口的关系 | 90000 token ≈ 128K 窗口的 70%；若模型是 64K 窗口则**直接超**。而代码里**没有任何地方知道模型窗口是多少**（`config.py` 无 context_window 字段）→ 这个 90000 是"拍下来的数字"，不是从模型能力推出来的 |
| 真实风险 | 单条 20 万字的历史（画布路径 assistant 消息 = 完整报告，`lead_dispatch.py:248`）可以原样穿过 `compress_history` |

**改法（S）**：新增 `LLM_CONTEXT_WINDOW_TOKENS` 配置（默认取模型真实值）+ 在 `assembler.for_agent_loop` 出口做一次 `estimate_tokens(messages)`（函数已存在，`context_utils.py:33-51`），超 `window*0.8` 时按 Priority 反向丢弃（`types.py:49-67` 的分级已经设计好了，只差调用）。**这一步不需要改任何 prompt**，只加阀门。

## 2.2 长会话：`history_summary` 四节结构 + 水位合并，什么时候丢关键口径

**触发条件（可精确计算）**

| 触发器 | 门槛 | 代码 |
|---|---|---|
| 未并入轮数 ≥ 6 | `LEAD_MEMORY_MERGE_ROUNDS=6` | `config.py:177`、`lead_agent.py:564,604` |
| **或** 未并入轮数 > 窗口 8 轮 | `WINDOW_MAX_ROUNDS=8`，即**第 9 轮滑出窗口时必须合并** | `lead_agent.py:105-122,603` |
| 单次合并最多 24 条消息 | `LEAD_MEMORY_MAX_MERGE_MESSAGES=24`，超出留到下轮（水位驱动） | `config.py:179`、`lead_agent.py:566,607-609` |
| 连续 3 次失败 → 熔断 | `LEAD_MEMORY_MAX_FAILURES=3` | `config.py:182`、`lead_agent.py:568-601` |

**丢口径的四条路径（按危险程度排序）**

1. **输出预算只有 600 字**：prompt 模板要求"总量不超过 {max_chars} 字"（`lead_agent.py:27-30`），`max_chars=LEAD_MEMORY_SUMMARY_TOKENS=600`（`config.py:185`、`lead_agent.py:47-50`）。四节要共享 600 字：分节配额是【口径与规则】300 /【关键数字】250 /【数据源】150 /【未决】100（`compress.py:107-112`，注意：这套配额在压缩层，`agent_loop` 里 `enforced=False` 未生效；**真正生效的是生成侧 prompt 的 600 字约束**）。**250 字装得下 4-6 个"数字+范围+来源"三元组**——超过就丢。这是最硬的天花板：**不是模型不会记，是物理上放不下**。
2. **合并输入被截断**：每条消息取前 800 字、整段取前 6000 字（`lead_agent.py:148-175`，默认 `per_msg_limit=800, total_limit=6000`）。24 条 × 800 = 19200 → **实际只看前 6000 字**，后面 2/3 的对话没进合并输入就"被水位跨过"。这是**静默丢失**（水位照推，`lead_agent.py:668-669`）。
3. **熔断降级会覆盖掉好记忆**：连续 3 次合并失败后走规则式兜底（`lead_agent.py:568-596`），返回 `{"summary": fallback}`，而 `chat_task._save_memory` 收到带 summary 的事件就 `repo.upsert(...)` **覆盖写**（`chat_task.py:128-138`）。兜底文本只含"用户首句 ≤80 字 + 带量纲数字"（`lead_agent.py:63-89`）——**口径 nuance 全丢，且不可恢复**。
4. **旧记忆挤新记忆**：合并是"旧摘要 + 新对话 → 重写 600 字"，prompt 要求"旧条目逐条保留"（`lead_agent.py:40-41`），但在 600 字上限下模型必须做取舍——**取舍不可控**（没有任何代码核验"上一版的口径节是否原样保留"）。

**什么时候最危险**：第 9 轮之后（窗口滑动）+ 单轮信息密度高（一次分析产出 20 个数字）→ 第 10 轮的【关键数字】只剩最后 4-6 个，前面 14 个消失，而**画布还在**（画布是外部记忆，救了视觉但不救数值叙述）。

**改法**
- **S**：把 `total_limit=6000`（`lead_agent.py:152`）提到 ≥ 单次合并输入的真实体量（24×800=19200），或**按 token 而非字符**设限（与 `budgets.py` 口径统一）。
- **S**：熔断兜底**不得覆盖**已有摘要——`_save_memory` 增加"来源=fallback 且已有非空 summary → 只追加不覆盖"（落点 `chat_task.py:128-138`）。
- **M**：【口径与规则】节独立存储（不被合并重写，只追加/去重），因为"口径错会让后面全错"（`lead_agent.py:30-31` 已写这个判断，但实现上是共享 600 字预算）。
- **M**：合并后做一次确定性回归检查：旧摘要里的"数字+单位"token（正则已有，`lead_agent.py:125,128-145`）若在新摘要中缺失，则在【关键数字】节强制回填。

## 2.3 一轮上下文到底多大（估算方法与实测）

**方法**（可复现，无外部依赖）：
1. 用仓库自己的 `estimate_tokens`（`context_utils.py:33-51`，CJK 1 字≈1 token，其余 4 字符≈1 token）作为唯一口径。
2. 构造与真实一致的状态块：画布用 `render_canvas_layout` 的上限 900 字（`canvas_tools.py:299`）、数据源用 `_load_available_datasources` 的真实形状（`lead_tools.py:401-409`）、窗口用服务端窗口上限 8 轮 × 2（`ai.py:154-174`）、指标用 `format_metrics_context` 的真实格式（`metric_service.py:273-277`）。
3. 让**真实的** `TurnContextProvider.for_agent_loop` 跑 5-6 步，每步打印 `Ledger.entries` 与 messages 总 token。

**实测（轻载：8 轮短消息 + 12 条指标）**

| 步 | 总 messages | 其中 system | 说明 |
|---|---|---|---|
| step0 | 3078 | 2576 | system 1899 + 记忆 77 + 数据源 41 + 画布 170 + 指标 303 |
| step1（1 次派发后） | 4189 | 3384 | + 子任务 569 + 评估 259 + 工具结果 259 |
| step3 | 4799 | 3388 | 每步约 +570（工具结果累积） |

**实测（重载：8 轮、assistant 消息为 2509 字报告、300 条指标）**

| 项 | token | 依据 |
|---|---|---|
| system prompt | **1899** | `ai_prompts.py:602`，实测 2849 字 |
| 工具 schema（10 个） | **1798** | `lead_agent_tools.py:136-138`，实测 4404 字 |
| 指标块（300 条） | **6348** | `metric_service.py:265-277` 无 cap |
| 窗口（8 轮 × 2 条，报告型） | **8126** | `ai.py:154-174` + `assembler.py:343` |
| 子任务块 | 146 | keep=2 |
| **单步合计** | **17,450 → 17,784** | 5 步实测 |
| **一轮（6 步上限）** | **≈ 10.6 万 token** | 17.7k × 6（`config.py:232`） |

**重要更正：任务书里"一轮 10 次工具调用 = 8000 token"的前提在本架构下不成立。**
`QUOTA = {observe: 2, dispatch: 3, repair: 2}`（`lead_agent_tools.py:49`）把非终止工具压在 ≤7 次/轮（终止工具不占配额）。且：
- observe 回执最多 2 条，元数据类结果保底 3200 token（`config.py:261`、`context_utils.py:92-95`）→ 最坏 6.4k；
- dispatch 回执只有一句话（`lead_agent_unified.py:543`）→ 约 0.1k。
**所以上下文炸弹不在工具结果（它被 800 token 收口，`assembler.py:299-312`），而在 metrics 块（6.3k，无界）和 window 块（8.1k，无界且 compressible=False）。** 修工具结果压缩是修不到点上的——这决定了 §3 的优先级。

**还有一条被忽略的增长通道：窗口在"一轮之内"也在长。**
`run_worker` 每次成功后把**完整报告**写进对话历史（`lead_dispatch.py:243-248`：`memory_text = result.report` → `ctx.add_turn("assistant", memory_text)`），下一句 `for_agent_loop` 重建时它已进入 WINDOW 块（`provider.py:67` 上限 30 条，`assembler.py:328-330` 原样直发）。即**每个派发步 +1 条完整报告（实测 991 token）**，3 次派发 = 一轮内 +3k token，且没有任何上限拦截。

**另一处隐性重复**：指标清单在 `ai.py:931-947` 会拼进本条 user 消息（≤4000 字时全量、超出时只给示例 key），同时 `ai.py:979` 又把**全量** `metrics_ctx` 交给系统块（`sources/state.py:92-106`）——指标 ≤4000 字时**同一份清单在 messages 里出现两遍**；>4000 字时则出现"user 消息里是摘要、system 里是全量"的不一致。

## 2.4 对标：Claude Code 的 compaction / Manus 的文件即记忆 / Cursor 的检索，该学谁

| 候选 | 核心做法 | 对我们适用度 |
|---|---|---|
| **Claude Code compaction** | 接近阈值时压缩历史 → 保留最近 N 条 + 摘要回填，**并把计划/todo 外置**以便压缩后恢复任务状态 | **最该学**。我们的窗口是"原文全量 + 无压缩"（`window.py:67`），等于把最贵的东西放在最不该放的位置 |
| **Manus 文件系统即外部记忆** | 中间产物写文件，上下文只放"路径 + 摘要"；**append-only / 前缀稳定**是第一指标 | **学它的工程纪律**（前缀稳定），因为我们已经为"system 中段改写"付了 10x 单价（见 2.5） |
| **Cursor 检索** | 不把所有内容塞进上下文，按需检索代码片段 | **暂不适用**：我们没有"海量代码库"问题；但**结构上等价**的需求是"按需取画布块详情/按需取数据画像"，可以晚做 |

**为什么不是"简单调大 token"**：
1. 调大只把崩溃点推后，**不改变每步全价重发**的结构——实测单步 17.7k、一轮 10.6 万，调大窗口 1 倍 = 账单翻倍而不解决任何问题。
2. 我们的缺失是三个**定性**问题，调参解决不了：①无界通道（metrics/window）②超窗无降级 ③前缀不稳定。
3. 有界性才是商品属性：客户要求的是"**最坏情况下**一次对话不超过 X 元"，"通常够用"不是 SLA。

## 2.5 前缀稳定性：`for_agent_loop` 每步重建 messages，system 前缀稳不稳

**核实结论：上一轮审计（`docs/research/02-orchestration-benchmark.md:20,113`）说得对，且可以量化。**

**机制**
1. 每步重建 messages（`assembler.py:266-350`）：`system = system_text + "\n\n" + ctx_lines`（`:337-338`）。`ctx_lines` 的**顺序由 Provider 的 source 字典顺序决定**：DATASOURCES → CANVAS → METRICS → SUBTASK → ASSESSMENT → ARTIFACT（`provider.py:73-80`），前面再挂 MEMORY（`provider.py:102`）。
2. 派发后 `provider.refresh_canvas(canvas_layout)` **替换 CANVAS 源**（`provider.py:95-97`，调用点 `lead_agent_unified.py:505-510`）→ system 中段内容变化。
3. 评审结论 append 到 `turn_summaries`（`lead_agent_unified.py:531`）→ SUBTASK/ASSESSMENT 块变化（`sources/state.py:109-132` 持活引用）。
4. **CANVAS 排在 METRICS 之前**（`provider.py:73-80`），而 METRICS 恒定且很大（实测 6348 token）→ 只要画布变了，**metrics + subtask + assessment + window + user + 全部工具结果**都失去缓存前缀。

**实测（同一轮 5 步，重载场景）**

| 步 | system token | 与上一步的**首个差异字符位** | 可缓存前缀 token | 可缓存占比 |
|---|---|---|---|---|
| step0→1 | 9470 | **4728**（在画布块内） | **2923** | 9470 的 31% / 整轮 17643 的 16.6% |
| step1→2+ | 9470 | 17789（子任务块尾） | 9470 | 本例第 3 步起摘要恰好等长所以整段稳定；**真实场景每次派发摘要都不同 → 差异点固定在画布块** |

轻载场景同样成立：step1 可缓存前缀 2220 / 总 4189（53%）。

**量化后果**（`llm_client.py:80-87` 已经在按次记录 `cached`，可在生产日志直接验证）：
- 现在：每步 17.6k 输入中约 **14.7k 走全价**（可缓存 2.9k）。
- 若把 CANVAS/SUBTASK/ASSESSMENT 移到 METRICS **之后**，且派发后的画布改走 **append-only 的 tool 结果**（而不是重写 system）→ 可缓存前缀 ≈ system 恒定段（≈8.9k）+ window（8.1k）+ 本轮 user + 上一步之前的全部工具结果 ≈ 17.5k。按缓存命中价约 1/10 计，**单轮输入成本可降 ~80%**。
- 这条**零行为变化**（prompt 语义不变，只是块顺序与承载方式变了），但它是"商品毛利"级别的差异。

**改法**
- **S**：`provider.py:73-80` 的 source 插入顺序改为"恒定块在前、易变块在后"（DATASOURCES → METRICS → CANVAS → SUBTASK → ASSESSMENT → ARTIFACT）。**一行 dict 顺序改动**，实测可把可缓存前缀从 2.9k 提到约 8.5k。
- **M**：派发后的画布快照不再 `refresh_canvas`，改为把"新布局"作为**一条 tool 结果**追加（`add_tool_result`，`provider.py:90-93`）。system 在整轮内逐字节不变 → 前缀 + append-only 完全成立。
- **S**：在 `ledger` 里加一个 `prefix_stable: bool`（对比上一步 system 的哈希），把"这轮前缀失效了几次"变成可观测指标（落点 `assembler.py:349` 返回值）。

## 2.6 轴 2 差距汇总与改法

| # | 差距 | 改法 | 规模 | 落点 |
|---|---|---|---|---|
| C1 | 无窗口对齐、超窗即整轮失败 | `LLM_CONTEXT_WINDOW_TOKENS` + 出口 token 检查 + 按 Priority 裁剪 + 400 时二次压缩重试 | **M** | `assembler.py:349`、`lead_agent_unified.py:287-296`、`config.py` |
| C2 | metrics 块无界 | `format_metrics_context(max_items/max_chars)`，超出走"示例 key + 提示 list_metrics"（`ai.py:939-945` 已有这套策略，只是没用在系统块上） | **S** | `metric_service.py:265-277`、`ai.py:979` |
| C3 | window 块无界且 compressible=False | 打开压缩：`MessageListPolicy` 已实现（`compress.py:80-103`），只差把 `window.py:67` 改 True + enforced | **S/M** | `sources/window.py:60-68`、`assembler.py:316` |
| C4 | 前缀每步失效 | 块顺序 + 画布改 append-only | **S/M** | `provider.py:73-80,95-97` |
| C5 | 熔断兜底覆盖好记忆 | fallback 不覆盖已有摘要 | **S** | `chat_task.py:128-138` |
| C6 | 合并输入截断 6000 字 | 提到与 24×800 相称，或改 token 口径 | **S** | `lead_agent.py:151-153` |
| C7 | 成本不可见 | 每轮把 `ledger.used_tokens × steps` 落一行可查询日志/指标（已有单步日志 `lead_agent_unified.py:268`） | **S** | `lead_agent_unified.py:557-565` |
| C8 | 两个出口是死代码 | `for_decision`/`for_answer` 无生产调用点（grep 证据）→ 删除或标注 | **S** | `provider.py:111-159` |

---

# §3 如果只能补 5 件事（按"客户会不会当场发现"排序）

| 序 | 事项 | 客户当场会看到什么 | 为什么排这个位置 | 规模/落点 |
|---|---|---|---|---|
| **1** | **超窗/超时兜底 + 硬上限**（C1） | 现在：**"服务暂时不可用，请稍后重试。"** + 画布没动 | 唯一一个**100% 会在客户现场变成"你们的 AI 坏了"**的故障；且发生条件与租户数据规模正相关（越大的客户越容易炸）。修完至少退化成"我压缩了历史并重试" | M · `assembler.py:349`、`lead_agent_unified.py:287-296` |
| **2** | **子任务结构化回执**（P1，1 行改动） | 现在：第 3 次派发起"重复出图""两次数字不一致" | 多步分析是主打卖点，**第二次派发就开始退化**；修法几乎是免费的（`exec_summary` 已在 `lead_agent_unified.py:523` 算好，只是第 543 行没给模型） | S · `lead_agent_unified.py:543` |
| **3** | **数据画像**（P3：行数/基数/空值/时间范围） | 现在：维度选错→饼图碎成 8 片、全表扫 800 万行超时、"1.2 万行"是编的 | 这是**数据产品 vs 聊天机器人**的分界线；一次客户演示就会被问"你怎么知道用哪个维度" | S · `lead_observe.py` + `lead_agent_tools.py:89-100` |
| **4** | **指标块与窗口块的有界化**（C2/C3） | 现在：指标多的租户每步 6.3k token 白烧，长会话窗口 8.1k 且永不压缩 | 客户不一定"当场"看见，但它是**账单和延迟**的直接来源；且与"越用越贵"的体感挂钩，第 2 个月一定被发现 | S/M · `metric_service.py:265-277`、`sources/window.py:67` |
| **5** | **前缀稳定（块顺序 + 画布 append-only）**（C4） | 客户看不见，**账单看得见** | 零行为变化、纯工程纪律；实测可缓存前缀 2.9k→8.5k+，单轮成本可降约 80%。放在第 5 是因为前 4 件不做会"炸"，这件不做只是"贵" | S/M · `provider.py:73-80,95-97` |

**没进前 5 但需要立项**：记忆覆盖保护（C5，长会话第 10 轮后口径丢失）、合并输入截断（C6）、动作账本（P2 的 M 版）、成本可观测（C7）。

---

# §4 附录：证据与复现

## 4.1 测量方法

- 估算器：`context_utils.estimate_tokens`（CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token）。
- 装配器：真实 `TurnContextProvider.for_turn(...).for_agent_loop(...)`，5-6 步模拟"每步派发一次"（`add_tool_result` + `turn_summaries.append` + `refresh_canvas`）。
- 合成输入对齐真实上限：画布 900 字（`canvas_tools.py:299`）、窗口 8 轮（`ai.py:154-174`）、指标按 `metric_service.py:273-277` 的真实一行格式、报告按 2509 字（约 30 条 bullet）。
- 工具 schema：直接 `lead_tool_specs()` 序列化后计 token。
- 单文件回归：`pytest tests/test_context_assembly.py` → **10 passed**（确认装配骨架的行为契约没被误读）。

## 4.2 关键实测值速查

| 量 | 值 | 来源 |
|---|---|---|
| LEAD_AGENT_SYSTEM | 2849 字 / 1899 token | `ai_prompts.py:602` |
| 10 个工具 schema | 4404 字 / 1798 token | `lead_agent_tools.py:136-138` |
| 轻载单步 | 3078 → 4799 token | 实测 |
| 重载单步 | 17,450 → 17,784 token | 实测 |
| 重载单轮（6 步） | ≈10.6 万 token | 17.7k × 6 |
| 当前可缓存前缀 | 2923 / 9470（31%） | 实测首个差异字符位 4728 |
| 指标块（300 条） | 6348 token，无 cap | `metric_service.py:247-277` |
| 窗口块（8 轮报告型） | 8126 token，compressible=False | `sources/window.py:67` |

## 4.3 未确认项（明确标注，不猜）

1. **deepseek-v4-flash 的真实上下文窗口**：代码中无任何常量（`config.py` 全量检索无 `context_window`/`max_context` 字段），故 §2.3 的"17.7k/步 是否安全"只能相对 `MESSAGE_LIST_MAX_TOKENS=90000` 讨论，**绝对阈值未确认**。
2. **线上真实前缀命中率**：`llm_client.py:80-87` 记录了 `cached`，但仓库内没有聚合/报表，**未确认**实际命中率分布。
3. **真实租户的指标条数与窗口消息长度分布**：无数据，§2.3 的重载场景是按代码上限构造的**上界**，不是均值。
4. **`query_sample` 在真实大表上的延迟**：走 `QueryDatasourceTool` + `QUERY_EXEC_TIMEOUT=30`（`config.py:55`），未实测。

---

## 一句话总结

**感知的问题不是"看不见"，是"看见了却只递上来一句话"——`RunAnalysisResult` 里的工具链、校验、报告被有意地留在了 `turn_summaries`，又在注入时被 keep=2/1000 字砍掉。**
**上下文的问题不是"预算没算"，是"算了却不执行"——分层、台账、优先级表都设计得很细（`types.py:49-67`、`budgets.py:141-187`），但 `enforced=False` 三个字让它们全部停在"文档"层，而真正的上限只存在于工具结果那 800 token 上。**
