# 04 · 对抗性评审：超级优化 MVP（含已落地 S0 提交）

> 评审员：red-team ｜ 共享任务：task-4 ｜ 日期：2026-11-08
> 立场：**默认假设方案是错的，直到验证它对**。所有结论都先打开代码/提交核对 file:line，不采信文档摘要。
> 环境：`backend/.venv`（Python 3.12.13 / fastapi 0.141.1 / starlette 1.4.1）；运行时配置读 `backend/.env`（本机：`LEAD_AGENT_MODE=single_agent`、`LANGFUSE_ENABLED=true`，该文件**不入库**——见 §1.1）。系统 `python`（3.13.9 / fastapi 0.115.6）**无法收集 7 个测试文件**（见 §1.3）。
> 纪律：只读代码与单文件测试；**未跑全量 pytest**；未改任何代码/分支。凡是"未确认"的都标出来。

---

## 0. 一页判决

### 0.1 五条决策

| 决策 | verdict | 一句话 | 关键依据 |
|---|---|---|---|
| **D1** 单循环 + 能力画像 | **修改** | 方向对，但画像只装"配置"装不下"判定语义"：现在的入口差异有 8 处在 `lead_tools.py` 的**评审/验收代码**里，不在配置里；不把它们搬进画像并加守卫测试，一年后必然长回"一个函数两套规则" | `lead_tools.py:171,176,183,610,618,700,799,831`；`lead_agent_unified.py:367-399` |
| **D2** 不引入 LangGraph | **保留（补漏）** | 结论对，但论据错了：缺的两项能力（并行扇出、节点级续跑）自研约 **400–600 行**，不是"1.5 万行迁移"；方案没有任何**重估条件/日期**，这是漏项 | §2.2 |
| **D3** 教学版单开子项目 | **保留** | 技术上与主链路隔离，无异议；但"B4 可与 2/3 并行"在单人开发下是假的，时间账要另算 | §4.3 |
| **D4** `agent_calls` 表 + trace_id + 6 插入点 | **修改** | 必须补三样：**读侧**（谁看/看什么/多久看）、**保留策略**（DB 无清理）、**插入点收敛**（用 contextvar 把 6 处收敛成 2 个核心钩子）；且要先修观测层"写进去没人读"的批量 bug | §1.2、§5.1、§5.3 |
| **D5** 拆 prompt + 能力画像 | **修改** | 要做，但**前提要修正**：`LEAD_AGENT_SYSTEM` 只在 `single_agent` 生效；本机 `backend/.env:32` 已切 single_agent，但 `config.py:233` 默认 `legacy`、tracked 的 `.env.example` 不含该项 → **交付包/新机器默认跑 legacy**，那里 prompt 早已按入口拆开（`lead_decider.py:426`）。画布专属规则占比按字符约 **20%**，不是"1/3" | §2.4、§1.1 |

### 0.2 五个批次

| 批次 | verdict | 一句话 |
|---|---|---|
| **B0 / S0** | **修改** | S0-1 安全（§3.1）；**S0-3 提交版是空操作**（`TraceRecord` 无 `update()`，异常被吞）；S0-2 降级判断正确但"挪到 B1 附带做"是错的（那是会话生命周期改造）；S0-4 保留；S0-5 **延后** |
| **B1 / S1** | **修改** | 先做 **S0.5 读侧（0.5 天）**，再砍成垂直切片：contextvar `trace_id` + `ToolExecutor` 钩子 + `recon_tool` 钩子 + turn 行 + CLI + **保留策略**。不做 JSONL 旁路、不做白名单单列、不做"5 处 usage" |
| **B2 / S2** | **拆分** | S2a（画像+拆 prompt+去死工具+锁差集，约 3 天）/ S2b（上下文源、translator 合并——**合并移出 MVP**）。S2-5 先修 memo key 否则是假功能；**DQ M1 必须单列批次**（它本身就是 2–3 天） |
| **B3 / S3** | **修改** | S3-1/S3-2 保留（便宜、可感知）；S3-3 **延后**（前置：错误码贯通 + 策略表测试）；**S3-4 砍掉**（已有编译期校验）；S3-5 延后 |
| **B4 / S4** | **保留** | 不进主链路，但别算进"3 周"；它占用的是同一个人力 |

### 0.3 如果只有 5 天：标准版 + **压力测试下的砍单顺序**

**5 天标准版（4 项，合计 ≈5 天）**：

1. **S2a（3 天）**：`capability.py` 画像 + 拆 prompt + chat 去掉 `get_canvas_layout` + `test_entry_profiles.py` 锁差集 + `test_entry_isolation.py` 棘轮（见 §0.4-7）。这是**唯一用户能直接看见**的产出（"两个助手不重叠"）。
2. **双跑对拍（0.75 天）**：`tests/agent_evals/dataset.jsonl` 已有 33 题（实测 33 行），跑 legacy vs single_agent，出 4 张表（派发分布/LLM 调用数/P50-P95 时延/degradation）。它要回答两个问题：**目标模式是哪个**、以及**仓库里两份互相矛盾的成本数据（§1.1）到底哪个对**；没有它，S3 的预算档位没有依据，S2a 的 prompt 是否退化也无法判定。
3. **S0.5 观测读侧（0.5 天）**（Lead 提议，我接受）：`observability.py:219-233` 把 `span.metadata` 打进本地日志 + 台账改 `logger.info` 摘要形态 + 测试断言日志内容。这是"能看见"的最低成本，比建表便宜一个数量级。
4. **B1-lite（0.75 天）**：contextvar `trace_id` + `ToolExecutor` 钩子 + turn 行 + `scripts/agent_trace.py`。**不含**保留策略/JSONL/6 个插入点（保留策略进 B1 完整版）。

**压力测试（时间不够时，按"从上往下砍"——顺序是明确的，不是三条并列）**：

| 剩余时间 | 保留 | 砍掉 | 理由 |
|---|---|---|---|
| 5 天 | 上述 4 项 | — | |
| 4 天 | S2a + 双跑 + S0.5 | **第一个砍：B1-lite** | B1 是基础设施；S0.5 已用 0.5 天拿到"看得见"。**双跑不能先砍**——它决定交付模式（§1.1）并验证 S2a 是否让 prompt 退化，砍了它 S2a 的收益就是薛定谔的 |
| 3 天 | S2a + S0.5 + 10 题 smoke（0.25） | 双跑降级为 smoke | 全量双跑 → 10 题只验"不比改前差"，不产出精确指标 |
| 2 天 | **只做 S2a** | 其余全砍 | S2a 是唯一用户可感知的产出 |

**明确不做**：`S2-6`（translator 合并）、`S3-4`（dry-plan）、`S0-5`、`S3-5`、S1 里的 4 个插入点、JSONL 旁路、`agent_calls` 保留策略（进 B1 完整版）。

### 0.4 与 Lead 的分歧与共识

| # | Lead 的判断 | 我的判断 | 结论 |
|---|---|---|---|
| 1 | S0-1 删 `[:4000]` 是安全的（所有 tool 消息都进 assembler） | **结论同意，理由只对一半。** 统一循环里确实没有 tool 消息绕过 assembler（`provider.py:90-93` → `assembler.py:292-312`，逐条独立压缩、确定性）。但"压缩层是唯一截断所有者"**不成立**：`compact_result_json` 对 `{"error": ...}` **原样返回、不截断**（`context_utils.py:87-88`），所以被删掉的 4000 字上限曾是"回喂模型的错误文本"唯一的界。今天风险≈0（工具错误都按 `str(e)[:200]` 构造，`agent_tools.py:749` 等），但这个洞应该由压缩层补"错误也封顶"，而不是夸成"唯一所有者"。legacy 路径从未有过这行，不受影响。 | **共识：没 bug；但文档表述要改** |
| 2 | S0-3 原来是空操作，`TraceRecord` 没有 `update()`；已改成 `trace.span().update()` 并加了回归测试 | **诊断完全正确**（提交版 `trace.update` 抛 AttributeError 被 `except: pass` 吞）。**但你的修法仍然是"写进黑洞"**：`TraceRecord.span()` 只 append 一个 `SpanRecord`（`observability.py:87-90`），它**不会**被桥接到 Langfuse（只有 `observe_llm_call`/`observe_tool_call` 才建 Langfuse 子对象），本地模式也不打印 `metadata`（`observability.py:210-233` 只打 name/latency/input/output/error）。你的回归测试断言的是"span 存在且 metadata 非空"，**锁的是机制不是可观测性**。全仓同类假调用不止 1 处，共 **6 处**（见 §1.2）。 | **分歧：修法不够，测试锁错了东西** |
| 3 | 基线 1137 collected / 1135 passed / 2 failed（chart_export 需要 chromium） | **完全同意，且我独立复现了**：`--collect-only` = 1137（`backend/.venv`）；`tests/test_chart_export.py` 2 failed；根因是 playwright 期望 chromium-**1228**、本机只装了 chromium-**1134**（`browsers.json` vs `%LOCALAPPDATA%\ms-playwright`）。测试只用 `pytest.importorskip("playwright")`，模块在、浏览器不在 → 失败而不是 skip。**所以"1136 passed"这条门槛在当前环境永远红**。 | **共识：门槛必须改写**（见 §1.3） |
| 4 | "6 个插入点"虚高，真正独立采集位 4 个；白名单分支在 `execute_tool_call` 内部不算独立点 | **大方向同意（4 类语义是对的），但两个方向都要修正**：① 白名单分支（`tool_executor.py:167-173`）和未知工具分支（`:175-178`）确实都在 choke point 内部，只要在函数入口/出口记账即可，**不该单列**；② 但"LLM usage"不是 1 处而是 **5 处调用点**，且 `react`/`canvas` 内核根本不取 usage；③ 更关键：`run_analysis` 有条**提前返回**的直接 `clear_canvas` 分支（`lead_tools.py:617-661`），它绕过 `ToolExecutor`，也绕过了方案里"run_analysis 收尾（:972-1001）"这个插入点 → 这是**第 7 条静默路径**，6 和 4 都没覆盖。**我的结论：4 类语义、5–8 个编辑点；治法是 contextvar + 一个 LLM 记账钩子（`llm_client._log_usage`，`llm_client.py:76-88,140` 无条件调用），而不是逐个补。** | **部分分歧：数目对，覆盖漏** |
| 5 | （追问）`agent_calls` 有没有 DB 端保留策略 | **没有，这是方案漏项。** 03 文档只写了 JSONL "7 天保留"（`03-codebase-audit.md:144`），DB 表没有任何清理/分区/汇总；按 §5.1 的写放大量级，单人 1–2 年、多用户 1 个月就会到需要治理的规模。 | **共识：算漏项** |
| 6 | （Lead 未问，我补充）方案针对哪个 `LEAD_AGENT_MODE` | 代码默认 `legacy`（`config.py:233`）、本机 `.env` 是 `single_agent`（`backend/.env:32`，未入库）、`.env.example` 不含该项。**方案没有一句话交代目标模式**，§5 的回滚开关也没有它。 | **新发现（§1.1）：必须在 §0 声明目标模式并写进交付校验** |
| 7 | 守卫测试改"棘轮"（`ALLOWED_ENTRY_BRANCHES` 快照）：新增 `entry ==/!=` 失败；快照条目被删也失败；capability/lead_worker 豁免 | **接受，数量也复核了：14 条是对的**（`agents/lead` 13 条 + `ai.py:360` 1 条；`ai.py:336/338/393/2252/2269` 是 SQLAlchemy 查询条件不是行为分支，匹配器要排除 `AISession.entry ==`）。但快照**不能按行号**——无关改动一挪行就假红，最后测试会被删。按 (file, 函数, 归一化行文本) 存；删条目时用 `--update` 一键重生成并在失败信息里打印新快照。再加一条：只有 `capability.py` 允许"entry → 行为"映射（不只是比较）。 | **接受（3 处修正：标识方式 / 一键更新 / 映射白名单）** |
| 8 | B1 之前先加 **S0.5 读侧**（0.5 天）：本地模式打印 span.metadata + 台账改 logger.info + 测试断言日志 | **接受，并建议补一点**：日志里要打 `ledger.summary()`（摘要）而不是整个 dict——metadata 里的 ledger 是嵌套 dict，`_summarize` 只截 500 字，直接打会丢结构；测试用 caplog 断言"每步一行 ledger_step"。这 0.5 天是整份计划 ROI 最高的一项。 | **接受（补"打摘要"要求）** |
| 9 | recon_tool 钩子不能砍：它是"Lead 到底看了什么"的唯一证据链 | **你是对的，我收回"可选"。** 理由比覆盖率强：用户原话是"toolchain 没有日志"，而观察工具恰恰是最需要解释动机的一类调用（`list_fields`/`query_sample`/`list_metrics`）。补充两点：① 钩子要记 `ok` + `degradation` 原因 + **结果摘要**（否则解释不了"看了什么"）；② 它只在 `single_agent` 有 observe 路径，但目标模式就是 single_agent（§1.1），所以"3 行换一条证据链"划算。 | **改判：保留（见 §5.3 修正）** |

---

## 1. 三个前提级发现（先看这三条，它们改变所有批次的价值判断）

### 1.1 【最严重】"生产模式"由一个**未入库的 `backend/.env`** 决定，代码默认是 `legacy`

> 我最初写的是"方案在优化非默认模式"，随后发现 `backend/.env:32` 已把它切成 `single_agent`。**结论没变，但性质变了**：不是"方案选错模式"，而是"**模式的选择只存在于一台机器的未入库文件里**"。

**事实链**：

| # | 证据 | 说明 |
|---|---|---|
| 1 | `config.py:227-233` | 注释原文："legacy = 四个 prompt 工位……**当前生产形态**"；`LEAD_AGENT_MODE: str = "legacy"` |
| 2 | **`backend/.env:32`** | `LEAD_AGENT_MODE=single_agent`（本机真实配置）；同处注释："2026-10-17 A/B：语义达成率同为 94.1%，调用数 139 < 187，默认切到 single_agent" |
| 3 | `.gitignore:83` | `backend/.env` 被忽略（`git check-ignore` 证实）→ **这份配置不入库** |
| 4 | `backend/.env.example`（tracked） | **不含** `LEAD_AGENT_MODE`（只有 `TASK_QUEUE_ENABLED=false`、`LANGFUSE_ENABLED=false`）→ 新机器/交付包按示例起服务 = **legacy** |
| 5 | `config.py:339` | `model_config = {"env_file": ".env"}` 是**相对路径** → 必须从 `backend/` 启动才读到这份 .env；从仓库根启动会静默回落默认值（legacy + Langfuse off） |
| 6 | `lead_agent.py:497-502` | 只有 `single_agent` 才进 `_stream_unified`；`lead_decider.py:27-38` 显示 legacy 只有 4 个动作、**没有 observe 工具** |
| 7 | `lead_decider.py:422-426` | legacy 的决策 prompt **已经**按入口拆成 `LEAD_MERGED_SYSTEM`/`_CANVAS`（且被测试锁住，`test_architecture_consistency.py:177-198`） |

**后果（这是整份 MVP 最该先回答的四件事）**：

1. **方案没写"目标模式是哪个"**。作者机是 `single_agent`，所以在这台机器上 S0/B2 都生效；但**交付包/新机器默认 legacy**——699 元的成品包卖出去，用户拿到的可能是"方案做完但产品没变"的版本。证据很硬：`.env.example` 缺这一项，`_ENV_FILE` 又是相对路径。
2. **回滚开关清单缺了最重要的一个**：§5 列了 `AGENT_CALL_LOG`/`LEAD_PROFILE_ENABLED`/`LEAD_AGENT_MAX_LLM_CALLS`，而真正能"一键回到当前行为"的是 `LEAD_AGENT_MODE=legacy`（且在 .env 里，切它要重启）。
3. **成本前提有两份互相矛盾的数据**：`backend/.env:32` 的 A/B 记录是 single_agent **139 < 187**（更少，所以切了默认）；代码注释（`lead_agent_unified.py:56-76`）却说 20 题单 Agent **多 47 次（+32%）**。方向相反、样本/口径不明。MVP §7 说"所有成本判断都建立在 20 题小样本上"，但仓库里明明还有一份方向相反的记录——**在跑双跑对拍把这两份数对齐之前，S3-1/S3-5 的预算档位没有可靠依据。**
4. **S1-3 插入点④（`recon_tool`）、S2-3"chat 里 `get_canvas_layout` 是死工具"、D5"prompt 泄漏"，都只在 `single_agent` 成立**；legacy 下这四条不存在（没有 observe 工具，prompt 已拆分）。

**替代方案（必须写进 MVP）**：
- §0 明确写"**目标模式 = `single_agent`**"，并把它变成**交付前置条件**：`.env.example` 增加该项 + 启动日志打印当前 mode（避免"发出去的包在跑 legacy"这种事故）；
- 把 `LEAD_AGENT_MODE` 加进 §5 的回滚开关表，注明"改它需重启进程、不能用热开关"；
- 先用 §0.3 的双跑对拍把 **139/187 vs +47 次** 对齐，再决定 S3-1/S3-5 的预算档位。

### 1.2 【批量 bug】观测层"写侧齐、读侧零"：`metadata` 是死数据，至少有 6 处

**事实链**：

| # | 位置 | 写法 | 后果 |
|---|---|---|---|
| 1 | `observability.py:74-109` | `TraceRecord` **没有 `update()`** | 8117070 的 `trace.update(...)` 抛 AttributeError，被 `except: pass` 吞（提交版原文） |
| 2 | `observability.py:87-90,92-104` | `trace.span()` 只 append `SpanRecord`；`finish()` 只把 children_count/latency 给 Langfuse | 直接创建的 span **不会**桥接到 Langfuse |
| 3 | `observability.py:210-233` | 本地模式只打 name/type/latency/**input/output/error**，**不打 metadata** | 本地模式写入的 metadata = 直接丢 |
| 4 | `lead_agent.py:446-448` | `_log_ctx_ledger`：`trace.span(...)` + `span.update(metadata=...)` | 同上，台账落进内存 |
| 5 | `react_agent.py:109-115,437-443` | `self.agent_trace.metadata.update({iterations / done_reason / tool 成败})` | 同上（`agent_trace=` 由 `lead_tools.py:873-874` 传入，就是 TraceRecord） |
| 6 | `agent_orchestrator.py:287-289,842-849` | `trace.metadata["degradation_reason"]` / `step_stats` / `total_steps` | 同上；03 文档 #61 只记了"metadata 不打印"，没意识到这是**全仓通用假调用** |
| 7 | `ai_service.py:487` | `trace.metadata["suggestion_count"]` | 同上（`observer.trace(...) as trace` → TraceRecord） |

**为什么这条改变 B1 的判断**：
- **本机 Langfuse 其实是开的**：`backend/.env` 里 `LANGFUSE_ENABLED=true`（带真实 key），`langfuse 2.60.10` 已装在 `.venv`；所以 `observe_llm_call`/`observe_tool_call` 建的 span **会上报**。但**直接 `trace.span()` 建的 span 不会**（Langfuse 子对象只在两个 helper 里创建，`observability.py:274-284,329-339`），本地模式也不打印 metadata（`:210-233`）。所以问题不是"没开 Langfuse"，而是"这些写法不在上报路径上"。
- 03 文档 B.3 的推荐 (b)"只把 Langfuse 打开就能解决 90% 痛点"：对 tool/llm span 成立，**对台账/step_stats/ledger 不成立**。
- S1 计划里的"LLM usage 补进 span"如果沿用同样写法，会落进同一个黑洞。
- Lead 的 S0-3 修复让"不崩了"，但台账仍然没有消费者 → 和 §2.3 说的"记了没人看"是同一个病。

**替代方案（成本 S，收益最大）**：先在 `observability.py` 补读侧，二选一或都做：
1. 本地模式把 `sp.metadata` 打进日志（`observability.py:219-233` 加 2 行；`_summarize` 已做截断）；
2. 给 `trace.span()` 加一个"桥接 Langfuse"的可选参数，或在 `TraceRecord.finish()` 逐 child 上报。
然后 S0-3 的台账改成 `logger.info("[lead_unified] ledger_step %d %s", step, ledger.summary())` 这类**有人读**的形态，测试断言日志/上报内容，而不是断言"SpanRecord.metadata 非空"。这条不修，B1 表的可查询性再强，也还是在"关灯修车"。

### 1.3 【门槛失真】"基线 1136 passed"在当前环境不成立

| 项 | 实测 | 说明 |
|---|---|---|
| 收集数 | **1137**（`backend/.venv -m pytest --collect-only -q`） | 与"1136"差 1，量级可信，但不是 1136 |
| 已知失败 | **2 failed**（`tests/test_chart_export.py::test_render_option_to_svg_uri_with_real_data` / `::test_canvas_export_html_to_pdf_with_svg_chart`） | 我实跑复现：`p.chromium.launch` 失败；playwright 期望 chromium-1228，本机只有 chromium-1134 |
| 系统解释器 | **7 个文件收集失败**（`TypeError: Router.__init__() got an unexpected keyword argument 'on_startup'`，fastapi 0.115.6 + 新 starlette） | `test_architecture_consistency.py`、`test_ai_session_entry.py` 等**恰好是安全/架构守卫**——用错解释器时它们不是红，是**根本没跑** |

**替代方案（可判定门槛）**：
1. 门槛写死解释器：`backend/.venv/Scripts/python.exe -m pytest`；
2. 先把环境依赖变成 skip 而不是 fail：`test_chart_export` 用 `importorskip` 之外再加"浏览器可执行文件存在"判断，或在 CI 镜像里 `playwright install chromium`；
3. 门槛写成"**1137 collected / 0 failed（已知环境缺失项 skip 且列出名字）**"，并把基线与环境指纹（python/fastapi/starlette/playwright 版本）一起记进仓库文件，而不是写死在文档里；
4. 新增迁移门槛：`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`（现在 22 个迁移**全部**有 downgrade，但**没有任何测试跑过**，见 §5.2）。

---

## 2. 决策层逐条攻击（A1–A4）

### 2.1 A1 · D1"不做双循环，改做能力画像"够不够？

**问题**：两个入口的**终止条件**确实本质不同，但方案的画像字段（`terminal={require_canvas_action, require_verified}`）**与现有代码的语义对不上**：

| 文档说 | 代码实际 |
|---|---|
| 画布终止条件 = 必须产出 `canvas_action`（`02:198`） | 这是**画布内核内部**的判据（`canvas_orchestrator.py:53-56,59-74`：`_MAX_NO_TOOL_STREAK` + `_has_canvas_action`），**Lead 循环不检查**；Lead 的终止动作只有 answer/ask_user/stop（`lead_agent_unified.py:367-399`） |
| 对话终止条件 = 回答 + 数字后验通过 | 数字后验对 chat 是 **SOFT**，不是硬闸：`lead_tools.py:168-172`（`entry != "canvas" or blocks==0` → `soft` "报告数字存疑"），SOFT 会走一次 LLM 裁量（`lead_review_stage.py:120-144`），可以被判 pass |
| 画布零落块 = HARD | 有豁免：`lead_tools.py:176-181` + `_has_canvas_mutation`（`lead_tools.py:128-144`）；清空/删块/改标题成功时零落块**不算失败** |

**反例/风险**：如果照 `02` §6.2 的骨架把 `terminal.require_canvas_action=True`、`require_verified=True` 实现成代码闸，会发生两件**行为变更**（不是重构）：① 聊天里"数字未过验"从"可裁量"变成"硬失败"，重派率上升；② 画布"清空画布"成功但零 canvas_action → 被判未达标，正好违反 `lead_tools.py:112-117` 那条豁免。方案把 S2 当"分化（零行为）"，实际是**改判据**。

**"一年后会不会仍是一个函数两套规则"**：会。证据：入口条件现在是 8 处 `if entry ==`（`lead_tools.py:171,176,183,610,618,700,799,831`，另有 `lead_agent.py:523,743`、`lead_decider.py:426`、`canvas_state.py:71`、`lead_intent_signal.py:95`），**全部在"判定/路由/接受"逻辑里，没有一处是配置**。只加 `capability.py` 不搬这 8 处，等于给"两套规则"换了个文件名。

**替代方案（写进 S2-1/S2-7）**：
1. 画像必须包含 `review_policy`（HARD/SOFT 分类、豁免条件），把 `assess_subtask_detail` 的 3 处 entry 分支搬进策略对象（或至少把 entry 传参改成 `policy` 参数）；
2. 加一条守卫测试：**在 `app/` 里 grep `entry ==` / `entry !=`，除 `capability.py`/`lead_worker.py`（派发表）外一律失败**——这是"表不是 if"唯一能活过一年的机制；
3. 加第三入口 dry-run 测试：注册一个假 profile，断言循环能跑通（真正测可扩展性，而不是只断言差集）。

### 2.2 A2 · D2"不引入 LangGraph"的论据与时效

**问题**：结论我认为对，但**论据偷换了概念**。方案用的是"重写 = ~1.5 万行迁移"（`03:246`）。而"多分支并行取数+合并指标""审批流挂几天续跑"这两件事的真实成本不是迁移，是**新增能力**：

| 能力 | 现状证据 | 自研增量估计 |
|---|---|---|
| 并行扇出 | `graph.py:33-156` 只有单前驱边；但 worker 层已有 `asyncio.gather`（`canvas_orchestrator.py:441-443`） | **约 60–100 行**（ready-set + gather + 结果归并）+ 测试 |
| 节点级断点续跑 | `graph.py:143-155` 的 `__steps__` 生成即丢；`event_stream.py:55` TTL 1h，恢复语义是"整轮重跑"（`ai.py:723-766`） | **约 250–400 行**（state 序列化 + 恢复入口 + **副作用幂等**——难的是最后一项，不是存储） |
| 审批挂几天 | 事件日志 1h TTL；`lead_session_state.py:82` 只有挂起槽 | 需要持久化审批实例（PG 表）+ 过期/撤回 + 恢复入口，**另算 1 周** |

**漏项（这是 A2 的答案）**：方案**没有写任何重估条件**——§4 只给了 D1 的"中途切分支信号"，D2 没有。对比 02 文档自己写了"中期 6–12 个月目标态是 B"（`02:26`），MVP 却把"不引入编排层"写成永久结论。

**替代方案**：给 D2 补两句可判定的重估条件 + 日期，例如：
- 触发 A：单轮出现"≥3 个独立分支分析需要合并成一个报告"的真实需求（有评测题/客户案例）；
- 触发 B：审批/确认需要在进程重启后 >1h 续跑；
- 触发 C：**2027-05-01 强制复查**（半年），复查时按"自研成本 vs 框架成本"重新算，而不是按"迁移行数"。

### 2.3 A3 · D4 是"记了没人看"的日志吗？

**问题**：目前**是**。方案给出的唯一消费端是 `scripts/agent_trace.py`（S1-6），没有任何 API/UI；而本仓**已有先例**证明这个产品会做审计读侧：`operation_logs` 有 `api/v1/audit.py` 全量查询 + 前端 `frontend/src/pages/Audit`。给 `agent_calls` 只配一个 CLI，等于把它定位成"开发者调试工具"，而不是"产品能力"。

**方案没定义的三件事**：谁看、看什么、多久看一次。

**替代方案**：
1. **写死三个每月动作**（否则不做）：每月看一次"失败率 top3 工具→修或删"、"memo 命中率 <X% 的派发→调缓存策略"、"p95 时延按 kind 分解→定下一个瓶颈"；
2. **最小读侧**：`GET /audit/agent-trace/{trace_id}` + 复用 Audit 页的一个 tab（或 Statistics 页一张卡）；不做 UI 也行，但要有**一条 SQL 模板**；
3. 加每日汇总表/物化视图（`agent_calls_daily`），让"看一次"是秒级，而不是扫 JSONB。

### 2.4 A4 · D5 拆 prompt 会不会打爆现有测试？成本多少？

**事实**（我逐行核过）：

| 测试 | 断言 | 拆 prompt 后 |
|---|---|---|
| `test_lead_p1_agent_tools.py:113-123` | `yaml("lead_agent_system") 解析文本 == LEAD_AGENT_SYSTEM`（**逐字节**） | 若 `LEAD_AGENT_SYSTEM = LEAD_CORE + CHAT_PROFILE`，要么改 yaml 内容、要么测试红 |
| `test_lead_p1_agent_tools.py:100-110,126-131` | 含 `ask_kind/confirm/clarify`、`写工具`、`add_chart_block` | 需要保证这些 token 落在 CHAT 侧（`写工具`/`add_chart_block` 是**通用安全规则**，必须留在 LEAD_CORE） |
| `test_lead_p1_ask_kind_guard.py:128-134` | `yaml 文本 == _LEAD_AGENT_SYSTEM_FALLBACK`（逐字节） | 拆成 3 个 yaml 就要 3 个 fallback，或让"组合常量"与"组合 fallback"一致 |
| `scripts/_sync_prompt_fallback.py:38` | `--create` 依赖硬编码 anchor `LEAD_AGENT_SYSTEM = _load("lead_agent_system", _LEAD_AGENT_SYSTEM_FALLBACK)` | 如果这行被改名/删除，同步脚本对**新** prompt 的 `--create` 失效（现有 3 个 prompt 不受影响） |

**成本**：拆分本身不难（`prompt_registry.py:79` 用 `glob("*.yaml")` 自动加载，新增文件无需改注册表）——**约 1–1.5 天**（3 个 yaml + 3 个 fallback + 组合常量 + 改 2 个测试 + 新增 `test_entry_profiles.py` + 跑 eval 验证行为没退化）。方案写 "M" 量级不算错，但**它把"行为变更"写成了"分化"**：拆完 chat 的 system 会少掉那 ~400 token 的画布规则，这是**行为变更**，必须配评测（而 §5 的合并门槛里**没有评测门槛**）。另外这条只在 `single_agent` 生效——按 `.env.example` 起服务的部署根本走不到这里（§1.1）。

**替代方案的测试策略**（比"词表断言"可靠）：
- 三段 prompt 各自与自己的 yaml 做逐字节断言（保留原测试精神）；
- 组合层做**结构化断言**：CHAT 组合里不含 `get_canvas_layout` 工具、不含 `KPI 卡片` 小节标题；CANVAS 组合含 `落块` 小节。用"小节标记"而不是散词表，避免"KPI"这种词在别的语境里误报。

---

## 3. 已落地改动复审（`8117070`，改的是 `lead_agent_unified.py`）

### 3.1 S0-1（删 `payload[:4000]`）：**结论：安全，不是 bug；但文档的理由有一半是错的**

**逐路径核对**（问"有没有哪条 tool 消息不进 assembler"）：

| 路径 | 是否进 assembler | 证据 |
|---|---|---|
| 统一循环的观察结果 | **是**：`step_msgs.append({"role":"tool","content": payload})` → `provider.add_tool_result(step_msgs)`（`lead_agent_unified.py:538`）→ `for_agent_loop` 生成 `BlockKind.TOOL_RESULT`（`provider.py:139-143`）→ `assembler.for_agent_loop` 逐条 `compress_tool_message(800 token)`（`assembler.py:292-312`，`compressible=False` 也不影响——它走的是 TOOL_RESULT 分支的专用压缩，不看 enforced） | 已核实 |
| 统一循环的未知工具/配额拒绝消息 | **是**：同样 append 进 `step_msgs`（`:405-406,412-413`） | 已核实 |
| 统一循环的派发结果（review_note） | **是**：`:535` | 已核实 |
| assistant 的 tool_calls 消息 | 是进 assembler，但 content=None，无截断问题 | `:304-313` |
| legacy 路径 | **不经过这段代码**（这行只在 `lead_agent_unified`），不受影响 | `lead_agent.py:497-502` |
| Worker 内部（react/orchestrator/canvas）的 tool 消息 | 不进 assembler，但有自己的上限（`_MAX_RESULT_CHARS`/`compress_history`） | `context_utils.py:25-28` |

**唯一值得记的洞**：`compact_result_json` 对 `{"error": ...}` **原样返回（`context_utils.py:87-88`）**，所以"错误结果"不受 800 token 压缩约束；被删掉的 4000 字上限曾经是它唯一的界。今天工具错误都按 `str(e)[:200]` 构造（`agent_tools.py:749,1400+`），所以实际风险≈0；但这说明"压缩层是唯一截断所有者"是**目标**不是**事实**。

**建议（S 级）**：在 `compress_tool_message`（`assembler.py:151-166`）里给"错误结果"加一个硬上限（如 8k 字符，保留 error+hint），这样"唯一所有者"才名副其实；顺带修 `compress.py:57-77` 的 docstring。

### 3.2 S0-3（每步 ledger 落 trace）：**提交版是空操作；Lead 的修复也不可观测**

- **提交版**：`trace.update(metadata={f"ledger_step_{step}": ...})`。`trace` 来自 `observer.trace(...).__enter__()`（`lead_agent.py:482-488`），类型是 `TraceRecord`；该类只有 `span/finish/latency_ms`（`observability.py:74-109`）→ **AttributeError** → 被 `except Exception: pass` 吞。同一 bug 在**原代码**的循环末尾就存在（`git show a4aa444:...` 仍是 `trace.update(metadata={"agent_loop_ledger"...})`），也就是说：**这个仓的"上下文台账"从来没有真正落到任何 trace 上**，一直只有 `logger.info` 的 summary（`lead_agent_unified.py:550` 工作区版）。
- **工作区修复**（`trace.span(...).update(metadata=...)` + 新测试）：让代码不崩了、测试绿了（我实跑：`test_lead_p1_unified_loop.py` 18 项通过含新增用例），但 metadata **仍然没有读者**（§1.2）。所以这条的验收标准"trace 里每步一份 ledger"**目前无法被任何人验证**（本机 Langfuse 开着，但这些 span 不在上报路径上；关着就更没有）。
- **对 B2 的回答**（任务清单 B2：metadata 累加会不会让 trace 膨胀）：
  - 提交版：不膨胀，因为根本没写进去；
  - 修复版：`trace.children` 每步一个 `SpanRecord`，上限 = `LEAD_AGENT_MAX_STEPS`（默认 6）+ 1 个 total，**有界**，不存在无限膨胀；
  - 未启用 Langfuse 时：metadata 只存在内存对象里，请求结束即丢（`TraceRecord.metadata` 是普通 dict），**不落盘、不打印**；启用 Langfuse 时这些直接 span 也没有对应的 Langfuse 子对象，同样不上报。

### 3.3 明确结论：`8117070` 有没有 bug？

**有，但不是 S0-1 的 bug，是 S0-3 的 bug**：

| 改动 | 判定 | 说明 |
|---|---|---|
| S0-1 删 `[:4000]`（模型侧） | **无 bug** | 模型侧所有 tool 消息都经 assembler 压缩；SSE 侧仍保留 4000 字（UI 预算），行为符合注释 |
| S0-1 保留 `payload[:4000]` 于 SSE | **无 bug** | 但注意：SSE 与 DB/agent_calls 的长度口径从此不同，S1-2 的 `result_preview` 若照抄 SSE 会少 4k 尾 |
| S0-3 `trace.update(...)` | **是 bug（空操作）** | AttributeError 被吞，台账从未落 trace；且**不是新引入**——原代码同样的假调用也存在，这次只是把一个假调用复制成了"每步一个" |
| S0-3 修复（工作区，未提交） | **修了一半** | 不崩了，但仍无消费者；建议改成"日志/上报"形态 + 补读侧，否则 S1 会继承同一个病 |

---

## 4. 批次与顺序（C1–C3）

### 4.1 C1 · 先 B1 是不是在"修看不见的问题"？

**是。而且比"修看不见"更糟：它在修一个用户看不见的模式。**

- B1 的验收是"8 个运维问题能答 7 个"——这是**运维价值**，用户无感；
- B2 的验收是"chat 工具表没有死工具、prompt 不再有画布规则"——这是**产品价值**，用户可感；
- 方案给 B1 的理由是"B2/B3 的效果必须用 agent_calls 度量"（§4）。**这条不成立**：B2 的验收（`test_entry_profiles.py`、工具差集、prompt 小节断言）全是**结构断言**，不需要 `agent_calls`；B3 的 S3-2 TTFT、S3-1 调用数才需要。把"B3 需要"当成"B2 需要"，是顺序错误的根源。
- 再加上 §1.1：**目标模式本身没写清楚**——本机是 `single_agent`，交付包默认 `legacy`；B1 的插入点④、B2 的"死工具/prompt 不分入口"都只在前者成立。先把"目标模式"钉死，这些价值判断才成立。

**我的排序**（与 §0.3 一致）：**S2a → 双跑对拍（钉目标模式 + 对齐成本数据）→ B1 垂直切片 → S3-1/S3-2 → 其余**。若要"先看得见"，正确的做法是**先把观测读侧修好（S 级）+ 双跑对拍（S 级）**，而不是先建表。

### 4.2 C2 · 回滚开关真的能回滚吗？

| 开关 | 能回滚什么 | 不能回滚什么 | 验证方法 |
|---|---|---|---|
| `AGENT_CALL_LOG=off|full` | 只回滚"写入行" | 回滚不了同一批的 SSE 字段变更（S1-5 的 `tool_call_id`）、也回滚不了 `ToolExecutor` 里的钩子（钩子还在，只是不写） | 加一条测试：flag=off 时 `record()` 不投递、SSE 仍带 `tool_call_id`（向下兼容） |
| `LEAD_PROFILE_ENABLED` | 回滚"取哪份 profile/工具集/prompt" | **回滚不了 S2-6**（代码被删）；也回滚不了 S2-5（schema 里已有 `constraints` 字段，模型会填）；而且两套路径同时存在 = 测试矩阵翻倍 | 要么 S2-6 不做，要么给"profile 开关"写双侧测试（on/off 各一套关键断言） |
| `LEAD_AGENT_MAX_LLM_CALLS`（0=不限） | 只回滚 S3-1 | S3-3（策略表）、S3-4（dry-plan）没有任何开关 | 需要额外开关，或明确"这几项就是改行为，靠单批 revert 回滚" |
| **（方案漏）`LEAD_AGENT_MODE=legacy`** | 能回滚 **B0/B2/B3 的大半** | 回滚不了 B1（表与钩子在两条路径都生效） | 这是最重要的开关，方案 §5 没列 |

**结论**：§5 的"每批一个独立开关，任何一批出问题关开关即回到当前行为"是**过度承诺**。真正可回滚的是"一个独立 commit + `git revert`"；env 开关只对"纯新增、无契约变更"的批次成立。建议把 §5 改成两级：**开关可关（软回滚）** / **必须 revert（硬回滚）**，并逐批标注。

### 4.3 C3 · 工作量估算：乐观还是悲观？

| 批次 | 方案 | 我的估计 | 判断 |
|---|---|---|---|
| B0 | 0.5–1 天 | 已落 2 项；剩下的 S0-4（重试分类）≈0.5 天、S0-5（degradation 计数 + 前端兼容）≈0.5 天 | **合理**（但 S0-3 那个假调用要真是 bug 修，不是 0） |
| B1 | 3–4 天 | **5–8 天** | 迁移（含 downgrade/测试）+ 记录器（队列/单写入者/丢弃计数）+ **5–8 个编辑点** + 错误码派生（要改工具错误契约，见 §5.6）+ CLI + 测试；且没有前端/接口 |
| B2 | 4–5 天 | **8–12 天（最可能翻倍）** | ① 拆 prompt 1–1.5 天；② 画像 + 工具子集 + 测试 1 天；③ **S2-6 translator 合并（M→L，且打红 2 个源码级契约测试，见 §5.3 附）**；④ **DQ M1 2–3 天**（`DATA_QUALITY_CLEANING_PLAN.md:276-282` 自己写的）；⑤ 上下文源/前端联动 |
| B3 | 4–5 天 | **2–4 天（砍掉 S3-4 后）** | S3-1/S3-2 各 0.5 天；S3-3 延后；S3-5 依赖评测 |
| B4 | 与 2/3 并行 | **不可能真并行**（同一人力） | 时间账要单独加 1–2 周 |

**最可能翻倍的是 B2**，而且不是因为"1136 个测试跑得慢"，是因为它把一个 **2 周的数据质量主线**塞进了 4–5 天的验收里。按本仓测试规模（1137 项、80 个测试文件、单文件含子进程的 `test_chart_export` 6 项 4.3s），每批跑一次全量不是瓶颈（**具体时长未确认**，我没跑全量）；真正的瓶颈是**回归面**：S2 动 `lead_agent_tools`/`ai_prompts`/`provider` 会牵动 20+ 个测试文件的 import 与断言（03 文档 C.3 列过清单）。

---

## 5. 技术方案逐条攻击（D1–D6）

### 5.1 D1 · `agent_calls` 写放大与保留策略

**量级测算**（假设标注）：

| 项 | 数值 | 来源/假设 |
|---|---|---|
| LLM 调用/轮（legacy） | ≈7.4 | `lead_agent_unified.py:56-76`：20 题单 Agent 多 47 次（+32%）→ legacy ≈147/20 |
| LLM 调用/轮（single_agent） | ≈9.7 | 同上 |
| 工具调用/轮 | 5–20 | `03-codebase-audit.md:199` 的量级判断 |
| 行数/轮 | **约 14–34**（tool + llm + dispatch + turn） | 上两行相加 |
| 单行体积 | args ≤2KB + preview 500 + error 500 → **≤3KB**，典型 0.5–1.5KB | 03 文档 A.3.3 |
| 每轮写入 | **7–100KB**（典型 10–30KB） | 14–34 行 × 0.5–3KB |

**结论**：单人桌面产品（50 轮/天 × 365）≈ **0.2–1.8GB/年**；若做多用户（100 用户 × 50 轮/天）≈ **50–110GB/年**。JSONB 的 TOAST + 3 个索引会让 vacuum 变重。**方案只写了 JSONL 7 天，DB 完全没有保留/分区/汇总**（`03:144` 只针对 JSONL）——这是实打实的漏项。

**替代方案**：
1. DB 只存"骨架"：`args_json` 只存白名单键 + 哈希，**全文只进 JSONL**（方案已有 JSONL，但把全文也塞 DB 是重复存储）；
2. 保留策略：`created_at < now()-30d` 的 tool/llm 行删除 + `turn` 行保留（turn 行才 1 行/轮，适合长期留存）；或按月分区 `PARTITION BY RANGE (created_at)`；
3. 每日汇总表 `agent_calls_daily(trace 数/工具数/失败数/token/时延分位)`，让"看"不扫明细。

### 5.2 D2 · 迁移 `0023` 的锁表风险与可回滚性

**攻击"锁表风险"：这条担心基本不成立**（诚实标注）。

- `CREATE TABLE` + 3 个索引：只新表上 ACCESS EXCLUSIVE，**不锁现有表**；
- `BIGSERIAL`：新建序列，无风险；
- 真正的锁点在**外键**：`REFERENCES users/ai_sessions/ai_messages` 会在被引用表上加 `SHARE ROW EXCLUSIVE`（阻塞写入，通常毫秒级；但 `ON DELETE SET NULL` 会给删除路径加检查成本）。如果在意，就先建表、后 `ADD CONSTRAINT NOT VALID` 再 `VALIDATE`；
- **可回滚性**：22 个迁移全部有 `downgrade`（我数过：`Select-String 'def downgrade'` = 22），但**没有任何测试跑过 upgrade/downgrade**（仓库没有 conftest 建库，测试不连 PG）。"写了 downgrade"≠"能回滚"。
- 另外：`models/` 里要新增 `agent_call.py` 并在 `models/__init__.py` 注册，否则 `Base.metadata.create_all` 风格的测试看不到这张表（本仓测试多用假 session，影响有限，但新表最好两边都有）。

**替代方案**：把 `alembic upgrade head && downgrade -1 && upgrade head` 加进 B1 的合并门槛（用 `docker-compose` 的 PG），这是唯一能证明"可回滚"的方式。

### 5.3 D3 · 为什么是 6 个插入点而不是 1 个 choke point？

**结论：既不是 6，也不是 1；而且方案少算了一条路径。**

| 方案编号 | 我的判定 |
|---|---|
| ① `ToolExecutor.execute_tool_call` | **是唯一 choke point**，且白名单拒绝（`:167-173`）与未知工具（`:175-178`）两个分支都在它内部 → 记账放在函数入口/出口即可，**不该单列 ②** |
| ③ `run_analysis` 收尾 | 必要（dispatch 行 + `tool_chain` 已有数据，`lead_tools.py:788-793` 白白丢），**但必须处理提前返回**：`lead_tools.py:617-661` 的直执行 `clear_canvas` 在收尾之前 `return`，只插收尾会漏（第 7 条静默路径） |
| ④ `recon_tool` | **保留（改判，见 §0.4-9）**：观察工具绕过 registry（`lead_observe.py:206-265`），是"Lead 看了什么才这么答"的唯一证据链；记 `ok`/`degradation`/`结果摘要`。只在 `single_agent` 生效——而目标模式就是它（§1.1） |
| ⑤ LLM usage "5 处" | 不是 1 处：`planner_agent.py:193-196`、`agent_orchestrator.py:892-896,939-943`、`lead_decider.py:363-369`、`lead_agent.py:1007-1011`；且 react/canvas **不取 usage**（`react_agent.py:152-154`、`canvas_orchestrator.py:394-396`）。**更便宜的治法**：`llm_client._log_usage`（`llm_client.py:76-88`）是**所有调用形态的唯一漏斗**（`complete` 第 140 行无条件调用），在这里读 contextvar `trace_id` 就能一次覆盖 5 处 + 流式路径 |
| ⑥ 两个任务收尾 | 必要（turn 行 + `message_id` 回填），但这是 **2 个文件**（`chat_task.py:479-525`、`canvas_task.py:193-230`） |

**修正后的方案**：`contextvars.ContextVar("trace_id")`（入口 set）+ 2 个钩子（`ToolExecutor.execute_tool_call` 出入口、`llm_client._log_usage`）+ 2 个收尾（chat/canvas task）+ `recon_tool`（**保留**，见 §0.4-9）。**编辑点约 5–8 个，语义采集点 3–4 类**。

**附：S2-6"合并三份 translator"的真实成本（方案标 M，我认为 M→L）**：
- `ai.py:1970-2164` 不是"翻译器"，是**整个画布任务体**（~195 行：载入历史、执行、事件翻译、落库、记忆回流、释放锁、超时包装）；
- 两条路径的生命周期不同：进程内 `asyncio.Task` + `wait_for` 超时（`ai.py:2390-2413`）vs ARQ worker（`worker.py:67-97`）；
- `canvas_task.py:10-15` 明确写了"**别共用一个函数**"，并给出理由（两套事件语义）；
- **两个测试会直接红**：`test_ai_session_entry.py:146` 断言源码里存在 `async def _canvas_chat_task(`；`test_canvas_resume_contract.py:118` 对 `_canvas_chat_task` 做 `ast.parse` 校验"先写日志后广播"。
- 建议：**移出 MVP**，单独立项，且先写"进程内/队列"双路径等价测试。

### 5.4 D4 · 脱敏：sqlglot 重 parse 的成本 与 `[omitted]` 的排障代价

- **成本**：可以接受。sqlglot 已是依赖（`requirements.txt:45`），`sql_guard` 本来每次 SQL 都要 parse；多一次 parse 对单条 SQL 是毫秒级，相对 DuckDB 查询可忽略。但要注意：**只有 SQL 类工具的参数里有 SQL**，不是"每次工具调用多一次 parse"；方案表述"每次工具调用多一次 parse"不准确，会让人高估成本。
- **`[omitted]` 的代价**：把 `args_json` 整体置空，排障时看不到"它到底想查什么"。而代码里**已经有**更细的信号：`referenced_sensitive` 返回命中的列名（`agent_tools.py:645-658`），错误 JSON 里也有提示。**替代方案**：存 `{"blocked_columns": [...], "sql_masked": "..."}`（只抹字面量、保留表/列结构），既满足"敏感值不落库"，又保留"被哪层拦、想查哪张表"的线索；真需要"完全无痕"时再整体 omit，用 `error_kind=sensitive_column` 兜底。
- 另：方案说"连接串/密码永不记录（只放行白名单键）"方向正确，且与 `credential_refs.py` 的既有约定一致（附录已核）。

### 5.5 D5 · B3 的 dry-plan：对"单 schema 单表宽表"有多大价值？

**这是我认为最应该砍的一项。** 方案要校验的三件事，**两件已经在执行前做了**：

| 方案说要校验 | 现有实现（都在真正执行 SQL 之前） |
|---|---|
| 表/列是否存在 | `query_engine.py:669` `_ensure_datasource_ready`；`:758-759,772` `_validate_fields`（字段不存在时错误里列出可用字段，`:245-247`）；`:785` `_build_where` 也校验筛选字段（`:380`） |
| 聚合与维度是否冲突 | `:762` `_validate_aggregations`（聚合白名单）、`:764` `_validate_measure_types`（SUM/AVG 对 VARCHAR 直接拦，错误码 `INVALID_MEASURE_TYPE`）；维度不参与聚合冲突（全部进 GROUP BY） |
| 时间范围是否越界 | **没有语义时间轴**：模型传的是 `WHERE date >= ...` 字面量，没有"时间范围"这一等概念可校验；"越界"未定义 |

**模型侧还有一个反证**：我们每个数据源在 DuckDB 里就是**单 schema 单表 `data`**（`query_engine.py:50-65` 从 `information_schema.columns ... table_name='data'` 取列；外部 PG/MySQL 也只有一张 `pg_table_name`，`:644,666-667`），**没有 join、没有多表**。所以 Wren AI 的 schema linking / dry-plan 里"选对表/选对 join 路径"的价值在我们的模型上≈0。

**唯一真实的增量**：把"展开后的 SQL"在**执行前**给用户/模型看（透明性 + 人工核对口径），以及校验**指标公式展开**（`query_engine.py:754-756` 明确说"表达式度量的字段已内联在 expression 中，**跳过字段校验**"——这里确实有洞，但很窄）。

**verdict：砍掉（作为"正确性原语"）；若要做，重命名为"SQL 预览/口径核对"，并按透明性功能立项（前端展示 + 确认），而不是校验器。**

### 5.6 D6 · "错误码 → 策略表"由谁维护？会不会三个月就烂？

**先纠正一个事实**：错误码**已经存在**，只是没被传出来。`query_engine.py` 已有 12 个结构化码：`INVALID_FIELD / INVALID_AGGREGATION / INVALID_OPERATOR / INVALID_FILTER / SENSITIVE_COLUMN / INVALID_MEASURE_TYPE / ENGINE_AST_BLOCKED / QUERY_EXECUTION_ERROR / NOT_FOUND / SCHEMA_NOT_READY / ATTACH_FAILED / CIRCULAR_DEPENDENCY`（`:143,159,163,245-247,264-266,380,385-387,410-412,418-420,664,749-751,766,818,825`）。但 `QueryDatasourceTool` 走的是**另一条路**（raw SQL + `_build_query_error` 自由文本，`agent_tools.py:746-784`），这些码**永远不会出现在工具结果里**。所以 S3-3 的真实工作量不是"加一张表"，而是：**统一错误契约（把 code 传出来）+ 定义策略 + 实现动作 + 测试锁表**。

**"会不会烂"的答案：会，除非有测试锁。** 具体机制：
1. 单一 `ErrorCode` 枚举（现在码散在 `raise` 字符串里）；
2. `test_repair_policy.py` 断言：枚举里**每个码**都有策略项；每个策略动作都是已实现函数；未知码 → 默认动作（不崩）；
3. 在 `agent_tools` 的每个 error 返回里带 `code`（这是 B1 的 `error_kind` 能可靠派生的前提——S1-6 说"全部从现有代码派生，不新增业务逻辑"，**这句话对工具结果路径不成立**：guard 分支只返回 `guard.reason`（`agent_tools.py:580`），layer 只在日志里；要做到 `error_kind=guard_l1/l2/l3` 必须改返回结构）。

**verdict：延后**，触发条件 = "B1 上线后 `error_kind` 分布能给出 top3 可修复错误"。同时把"错误码贯通"作为 B1 的一部分先做（比 S3-3 便宜且是 B1 的必需项）。

---

## 6. 漏项（E1–E4）

### 6.1 E1 · 数据体检/清洗是不是漏了主线？

**不是"完全没提"，是"提了但没排期"——性质更糟。**

- MVP 确实提到：S2-4 的验收写"数据体检（`DATA_QUALITY_CLEANING_PLAN.md` M1）作为 chat profile 的**第一个新能力**接进来验证画像可扩展"（`SUPER_OPTIMIZATION_MVP.md:106`）；§6 明确不做清单也说"清洗已有独立计划（派生数据源 + 9 算子 + 干跑）"（`:163`）。
- 但 `DATA_QUALITY_CLEANING_PLAN.md` 自己是 **2 周**的计划（`:12`：M1 2–3 天 / M2 3–4 天 / M3 3–4 天），M1 还包含 REST 两个接口 + 前端体检卡 + 详情区块（`:253-282`）。
- 所以 MVP 的做法是：**把一条 2 周的主线藏在 B2 的"验收"里**。若严格执行 B2=4–5 天，DQ M1 必然被砍；若把 DQ M1 拉进来，B2 就翻倍（§4.3）。两种情况都不好。
- **这正是用户说的"对话助手要有清洗能力"**：它是产品主线，不是画像的"扩展性验证道具"。
- 顺带：`DATA_QUALITY_CLEANING_PLAN.md` 放在**仓库根**，而其他文档都在 `docs/`；`.gitignore:176` 只忽略 `docs/`，所以根目录这份是 tracked 的（`git ls-tree e77e8f7` 可见）。文件位置不一致会让"docs 里找方案"的人漏掉它（我自己第一次就只在 docs/ 下找，没找到）。

**替代方案**：DQ M1（体检 + `inspect_dataset` + 卡片）单列为 **B2.5**，给出的收益是"用户点名要的能力"；如果只能做一条线，**先 DQ M1 再 B1**（见 §0.3 的取舍理由——但注意 DQ M1 依赖 `lead_agent_tools` 的观察工具表，若 S2a 先落地，DQ 工具能直接进 profile，顺序反而更顺）。

### 6.2 E2 · 前端改动量

- **B1/B2 基本没有前端改动**：S1-5 加 `tool_call_id` 是**加字段**，两个 store 的 `switch` 对未知字段无感（`aiChatStore.ts:234-373`、`canvasAssistantStore.ts:168-347`）；S2 只改服务端 prompt/工具集。所以"B2 牵动两个 store"这个担心**不成立**（附录已核）。
- **真正的漏项在 DQ 主线**：`DATA_QUALITY_CLEANING_PLAN.md:268-272` 要 3 个**新 SSE 事件类型**（`quality_report`/`cleaning_plan`/`cleaning_result`）+ 体检卡 + 派生徽章 + 详情区块。而本仓的 SSE 中继是**白名单**——漏一个分支事件就静默消失（`test_lead_p1_ask_kind_guard.py:139-149` 的注释与断言明确记着"踩过两次"）。MVP 全文没有提这三个事件类型。
- 结论：**E2 的答案是"B1/B2 不用改前端，但 DQ M1 要改 5 处前端 + 3 个事件白名单"**，而 MVP 把 DQ M1 放在 B2 验收里却没算这笔账。

### 6.3 E3 · CI/回归成本

- 规模：**1137 项收集 / 80 个测试文件**（实测）；`test_chart_export` 6 项含 2 个子进程渲染，4.3s——说明**子进程/真机类测试**是耗时大头。全量时长**未确认**（按约束没跑）。
- 更贵的是**评测回归**：`tests/agent_evals/dataset.jsonl` 33 题 + `dataset_v2.jsonl` 30 题，`run_eval.py` 走**真实 LLM + 真实 DB**（`run_eval.py:117,190`），一次跑要几十道题的模型调用（时长/成本**未确认**）。方案 §5 的合并门槛里**没有评测门槛**——而 B2 改 prompt 是**行为变更**，恰恰最需要它。
- 建议门槛：每批 = 全量 pytest（pinned venv，0 failed）+ **30 题 eval 单跑**（记录通过率与成本，允许 ±5% 波动）；日常迭代用 `-k "lead or context or tool"` 子集（约 1 分钟级）。

### 6.4 E4 · 可量化的验收指标

方案 §7 列了 5 个基线数字（**这是优点**，比多数方案强），但**每批的验收标准仍是结构性的**（"工具表消失"、"能回答 7 个问题"），没有"优化前/后"的三个数字。建议明确写死：

| 指标 | 采集 | 改前/改后目标 | 用在 |
|---|---|---|---|
| 单轮 LLM 调用数（按 `trace_id` 聚合） | `agent_calls` | B2 后 **≤ +10%**（prompt 变短，调用数不应升） | B2 |
| 工具失败率 + top `error_kind` | `agent_calls` | B3 后失败率 **-20%**（相对基线） | B3 |
| TTFT P50（首步流式） | turn 行时间戳 | B3-2 后 **-30%** | B3 |
| eval 通过率（33 题） | `run_eval.py` | 任何一批 **不下降**（±5% 内） | 全部 |

**注意**：这些指标需要"同一批题、同一模型、同一天"跑两次才有可比性；只跑一次数字没有意义（模型随机性 + 数据变化）。

---

## 7. 方案作者（和我）可能没想到的 8 条

1. **"生产模式"只存在于一台机器未入库的 `backend/.env` 里**（§1.1）：`config.py:233` 默认 `legacy`、`.env.example` 不含 `LEAD_AGENT_MODE`、`_ENV_FILE` 还是相对路径（`config.py:339`）。作者机是 `single_agent`（所以 S0/B2 在本机生效），但**交付包/新机器默认 legacy**——这是个"works on my machine"的产品级陷阱，而 §5 的回滚开关里偏偏没有它。
2. **仓库里有两份方向相反的成本数据，方案只引用了其中一份**（§1.1）：`backend/.env:32` 的 A/B 记录是 single_agent **139 < 187**（更省，所以才切默认）；`lead_agent_unified.py:56-76` 的注释是 20 题 **+47 次（+32%）**（更贵）。S3-1 的总预算闸、S3-5 的"复杂度→预算档位"到底该按哪个数定档，方案没有交代。
3. **观测层是批量假调用，不止 S0-3 那一处**（§1.2）：`trace.metadata[...]` / `trace.metadata.update(...)` / `trace.span(...).update(metadata=...)` 共 6 处，本地模式不打印、Langfuse 模式不上报。所以"打开 Langfuse 就能看"（03 文档 B.3 推荐 (b)）只对 tool/llm span 成立，对台账/step_stats 不成立；S0-3 的"修好"只是从"崩"变成"静默丢"。
4. **S2-5 的 `constraints` 是"假透传"**：`run_analysis` 只消费 `constraints["mode"]`（`lead_tools.py:610`，全仓唯一读取点）；而 memo 幂等键**不含 constraints**（`lead_tools.py:81-85` 只拼 goal/datasource/canvas/entry）。给模型新增 `source/time_range/output` 三个字段却不消费，等于给它一个假承诺；更糟的是"只改 constraints 的重派"会**命中旧 memo** 返回旧结果——正是 `lead_tools.py:213-231` 注释里记录过的 P2.5.1 坑的翻版。
5. **"两个入口的终止条件本质不同"是文档的想象，不是代码的事实**（§2.1）：chat 的数字后验是 **SOFT**（可被 LLM 裁量放行），canvas 的零落块是 **HARD 但有豁免**，Lead 循环本身对两者都不检查。画像里 `terminal` 字段照抄配置表会**把软规则实现成硬闸**。
6. **S2-6 会打红两个"源码级契约测试"**（§5.3 附）：`test_ai_session_entry.py:146`、`test_canvas_resume_contract.py:118` 直接断言 `_canvas_chat_task` 的存在与结构；而且 `canvas_task.py:10-15` 明确写了"别共用一个函数"。方案却把它当成"删一份重复实现"。
7. **`trace_id` 的粒度是"请求"不是"轮"**（S1-1 的隐含假设）：HITL 确认是**第二次 POST**（`ai.py:723` 的 `body.resume` / `ui_action`），会产生新的 `trace_id`；"这轮为什么没出图"在"确认卡片→用户点确认→续跑"的链路上会跨两个 trace。要回答这类问题，`agent_calls` 需要 `turn_id`（或让 confirm 续跑复用原 trace_id，用 `parent_span` 连起来）。方案只写了 `trace_id`。

8. **"太严的守卫 = 会被删的守卫"**（Lead 在评审我时反过来抓到的一条）：我最初提议"app/ 里 `entry ==` 一律失败"——但存量 14 处、S2a 只有 3 天，这条测试一加就红，最后只能被删或被豁免，**守卫死了比没有守卫更糟（它制造了"有守卫"的错觉）**。正确形态是**棘轮**：新增泄漏失败、存量清单只能减不能增。同理，MVP §5 的"1136 passed"也是"太严且测不了"——一个永远红的门槛等于没有门槛（§1.3）。这条可以推广到整份清单：**每个"必须过"的门，都要同时给出"今天怎么过"的机制。**

（备选，不够硬但值得记：`docs/deliverables/lvco-bi/` 是一份 7 月的**整仓旧拷贝**（含自己的 `alembic/versions`、`tests`、`node_modules`，且没有 `lead_agent_unified.py`）。任何"文档说 X 在 file:line"的核对，都可能 grep 到这份旧拷贝而被误导；我的核对一律只认 `backend/` 下的版本。建议交付目录移出仓库或加 ignore。）

---

## 8. 附录 A：核对后"文档没说错"的条目（诚实清单）

> 说明：以下都是我**带着怀疑去核**、结果**文档是对的**。列出来是为了让作者知道哪些论点可以放心用。

| # | 文档断言 | 核对结果 |
|---|---|---|
| 1 | `config.py:233` 默认 `LEAD_AGENT_MODE=legacy`、`:235` 步数 6、`:259` `TOOL_RESULT_MAX_TOKENS=800` | **全部正确**（`config.py:233,235,259`） |
| 2 | `ToolExecutor` 是工具执行唯一 choke point，构造函数 keyword-only | **正确**：`tool_executor.py:102-115,152`；三条编排路径都构造它（`react_agent.py:336`、`agent_orchestrator.py:500`、`canvas_orchestrator.py:351`） |
| 3 | SSE `tool_call`/`tool_result` 没带 `tool_call_id`，但 `tc` 里有 | **正确**：`tool_executor.py:162,189,202,36` |
| 4 | 工具消息两条路径都进 assembler（provider 原样存 → assembler 逐条压） | **正确**：`provider.py:90-93,139-143`；`assembler.py:292-312` |
| 5 | 压缩层"只压不取"、`enforced=False` | **正确**：`compress.py:199-203`；`budgets.py:116-137` |
| 6 | 写工具永不注册 + 5 道门 + 6 个测试守卫 | **正确**：`lead_agent_tools.py:25-31`；`test_architecture_consistency.py:53-131` |
| 7 | `ENTRY_WORKERS` 是表驱动（canvas: canvas/react；chat: orchestrator/react） | **正确**：`lead_worker.py:43-46` |
| 8 | chat 下 `get_canvas_layout` 是死工具 | **正确**：`canvas_state.py:69-71`（`entry=="canvas"` 才 enabled）→ `lead_observe.py:27-38` 恒返回空串 |
| 9 | `LEAD_AGENT_SYSTEM` 的 yaml 与 fallback 有逐字节测试 | **正确**：`test_lead_p1_agent_tools.py:113-123`；`test_lead_p1_ask_kind_guard.py:128-134` |
| 10 | Langfuse 默认关、日志只到 stdout、Redis 事件日志 1h/2000 条/每轮清空、DB 只存正文 | **代码默认层面全部正确**：`config.py:119`、`main.py:15`、`event_stream.py:44,55`、`ai.py:1020`、`chat_task.py:491-497`。**但注意**：本机 `backend/.env` 把 `LANGFUSE_ENABLED` 设为 true 且 key 是真的（`langfuse 2.60.10` 已装），所以"作者机"上并非零留存——"默认部署零留存"只对**新装/`.env.example`** 成立。 |
| 11 | 画布内核用 `canvas_action` 作"达成"判据、有 `_MAX_NO_TOOL_STREAK` | **正确**：`canvas_orchestrator.py:53-74`（但这是**内核内**判据，不是 Lead 终止条件，见 §2.1） |
| 12 | 迁移 head = `0022_add_credential_ref` | **正确**：`backend/alembic/versions/0022_add_credential_ref.py`；22 个迁移**全部**有 `downgrade` |
| 13 | DDL 里的 UUID 外键类型 | **正确**：`users.id`/`ai_sessions.id`/`ai_messages.id` 都是 UUID（`models/user.py:20`、`ai_session.py:18`、`ai_message.py:20`），与 DDL 一致；（我原本怀疑这里是错的） |
| 14 | sqlglot / langfuse 已是依赖 | **正确**：`requirements.txt:45,67` |
| 15 | `ai.py:667/1723` 是两入口，`:1020` 清事件日志，`:1970-2164` 是内联画布任务体 | **正确**（行号对得上） |
| 16 | 三份画布事件翻译器 | **正确**：`chat_task.py:362-462`、`canvas_task.py:73-156`、`ai.py:2020-2094`（后两者同构） |
| 17 | 20 题双跑 +47 次调用（+32%） | **与代码注释一致**：`lead_agent_unified.py:56-76`（但如 02 文档自己所说，是小样本；且与 `backend/.env:32` 的 139<187 方向相反，见 §1.1） |
| 18 | `.gitignore:176` 忽略 `docs/`，文档需 `git add -f` | **正确**；且 e77e8f7 确实把 docs 强制入库了（`git ls-tree e77e8f7` 可见） |
| 19 | "lock 表风险"（我攻击过） | **这条担心被高估**：新建表 + 索引不锁存量表；只有外键校验会在被引用表上短暂加锁（见 §5.2） |
| 20 | ".env 是本项目的配置源"（`config.py:17-18`） | **正确**，但有两个副作用：① `backend/.env` 被 `.gitignore:83` 忽略 → 配置不入库（§1.1）；② `env_file=".env"` 是相对路径，从仓库根启动会读错文件 |
| 21 | `LEAD_AGENT_SYSTEM` 只在统一循环生效 | **正确**：全仓只被 `lead_agent_unified.py:226` 引用，legacy 走 `lead_decider` 的 merged/decision/answer 三份 prompt |
| 22 | "画布专属规则约占 1/3 篇幅" | **我实测高估**：system 正文 2983 字 / 76 行，画布强相关 12 行、591 字 ≈ **19.8%**（est ≈1933 token 里约 396 token）。方向对，量级高估约 1.7 倍 |
| 23 | 三份 translator 的划分 | **正确**：`chat_task.py:362-462`、`canvas_task.py:73-156`、`ai.py:2020-2094`（`canvas_task.py:10-15` 还写了"别共用一个函数"的理由） |

---

## 9. 附录 B：未确认项 与 复核命令

**未确认**（不要当结论用）：

1. 全量 pytest 的实际耗时（按约束未跑；只有收集 2.1s、单文件 4.3s 的样本）。
2. `run_eval.py` 一次 33 题的真实耗时/成本（需要真实 LLM + DB）。
3. B1 在真实负载下的写放大（14–34 行/轮是**估算**，不是实测；本仓没有任何"每轮工具调用数"的现成统计——这本身正是 D4 要解决的问题，属于循环论证，要承认）。
4. 03 文档"39 个文件 14,841 行"的迁移面（我没逐一重数）。
5. `docs/deliverables/lvco-bi/` 旧拷贝是否会污染 pytest 收集（只在 `backend/` 下跑就不会；从仓库根跑会）。

**复核命令**（都在 `E:\BI\LvcoBI\lvco-bi`）：

```powershell
# 1) 收集数与 chart_export 的两条失败
cd backend; .venv/Scripts/python.exe -m pytest --collect-only -q | Select-Object -Last 3
.venv/Scripts/python.exe -m pytest tests/test_chart_export.py -q      # 期望 2 failed / 4 passed
.venv/Scripts/python.exe -c "import playwright,json,pathlib;d=json.loads((pathlib.Path(playwright.__file__).parent/'driver'/'package'/'browsers.json').read_text());print([(b['name'],b['revision']) for b in d['browsers'] if b['name']=='chromium'])"
Get-ChildItem $env:LOCALAPPDATA/ms-playwright                                  # 本机只有 chromium-1134，playwright 要 1228

# 2) S0 相关单测（工作区版）
.venv/Scripts/python.exe -m pytest tests/test_lead_p1_unified_loop.py tests/test_lead_p4_tool_result_compression.py -q

# 3) 8117070 的 diff 与 TraceRecord 缺 update
git show 8117070 -- backend/
Select-String -Path app/services/observability.py -Pattern 'class TraceRecord|def update|def span'

# 4) 死 metadata 全扫
Select-String -Path app/services -Recurse -Pattern 'metadata.update|.metadata['

# 5) 模式：代码默认 legacy、本机 .env 是 single_agent、.env.example 缺该项
Select-String -Path app/config.py -Pattern 'LEAD_AGENT_MODE|env_file'
Select-String -Path .env -Pattern 'LEAD_AGENT_MODE|LANGFUSE_ENABLED'
Select-String -Path .env.example -Pattern 'LEAD_AGENT_MODE|LANGFUSE_ENABLED'
git check-ignore -v .env
Select-String -Path app/services/agents/lead/lead_agent.py -Pattern 'lead_agent_mode|_stream_unified'
Select-String -Path app/services/agents/lead/lead_decider.py -Pattern 'class ActionType' -Context 0,10

# 6) dry-plan 已有校验
Select-String -Path app/services/query_engine.py -Pattern '_ensure_datasource_ready|_validate_fields|_validate_aggregations|_validate_measure_types'

# 7) S2-5 constraints 的唯一消费点 + memo key
Select-String -Path app/services/agents/lead/lead_tools.py -Pattern 'constraints.get|def make_idempotency_key|def effective_memo_key'

# 8) 两份互相矛盾的成本数据
Select-String -Path .env -Pattern 'A/B|调用数'
Select-String -Path app/services/agents/lead/lead_agent_unified.py -Pattern '多出 47 次|双跑对拍'
```

---

### 最后一句（给决策者）

这份方案的技术判断**大部分是对的**（不换框架、不拆双循环、先补可观测、prompt 要拆），真正的问题集中在**三件事**：**① 它没交代目标模式，而模式只活在一台机器未入库的 `.env` 里（代码默认 legacy、`.env.example` 缺项、交付包默认跑 legacy）**；**② 它把"记日志"当成"可观测"，而这个仓的观测读侧是空的（6 处假调用 + 本地不打印 metadata）**；**③ 它把一条 2 周的数据质量主线和一次 M-L 的 translator 重构，藏进了两个"4–5 天"的批次里**。把这三件事在文档里改对，方案的执行风险会立刻降一个数量级；技术路线本身不需要推翻。
