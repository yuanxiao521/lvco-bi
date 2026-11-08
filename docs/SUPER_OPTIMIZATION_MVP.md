# 超级优化 MVP · v2（对抗性评审后修订）

> v1：2026-11-07（三份调研汇总）｜ **v2：2026-11-08（red-team 对抗性评审后）**
> 评审依据：[04-adversarial-review.md](research/04-adversarial-review.md)（517 行：9 条分歧裁决 + 8 条盲点 + 23 条"核过但文档没说错"的诚实清单）
> 纪律不变：**不换跑道，只补短板**；每批可独立合并；**稳定优先于炫技**。v1 全文见 commit `e77e8f7`。

## 0. v1 → v2 改了什么（10 处，先看这张表）

| # | v1 | v2 | 谁提的 | 为什么（证据） |
|---|---|---|---|---|
| 1 | 没交代**目标模式** | 目标模式 = **`single_agent`**，升级为交付前置条件 | red-team §1.1 | `config.py:233` 默认 `legacy`；`backend/.env:32` 才是 single_agent 且**未入库**；`.env.example` **缺这项** → 交付包在客户机跑 legacy，整份清单白做 |
| 2 | B0→B1→B2→B3 | **S0.5 读侧 → S2a 画像 → 双跑对拍 → B1-lite** | red-team §4.1 | v1 说"B2 的效果要用 agent_calls 度量"不成立：B2 验收全是结构断言 |
| 3 | 6 个插入点 | **contextvar + 2 钩子 + 2 收尾 + recon** | 双方 | 白名单/未知工具分支就在 `execute_tool_call` 内（`tool_executor.py:167-178`） |
| 4 | 门槛 = 1136 passed | **pinned venv：1138 collected / 0 failed** + eval ±5% + 迁移往返 | red-team §1.3 | 实跑 **1138 collected / 1136 passed / 2 failed**（playwright 要 chromium-1228，本机 1134）；旧门槛永远红 |
| 5 | S3-4 dry-plan | **砍** | red-team §5.5 | `_validate_fields`/`_validate_aggregations`/`_validate_measure_types` 已在执行前做；我们是单 schema 单表 `data`，schema linking 价值≈0 |
| 6 | S3-3 错误码策略表 | **延后**（带触发条件） | red-team §5.6 | 错误码**已有 12 个**（`query_engine.py`），但 `QueryDatasourceTool` 走另一条路 → 真工作量是统一错误契约 |
| 7 | S2-6 合并三份 translator | **移出 MVP** | red-team §5.3 附 | 会打红 `test_ai_session_entry.py:146` / `test_canvas_resume_contract.py:118`；`canvas_task.py:10-15` 明写"别共用" |
| 8 | 画像只装配置 | 画像装 **`review_policy`（判定语义）** + **entry 分支棘轮** | red-team §2.1 + Lead 改良 | 入口差异有 **8 处在 `lead_tools.py` 评审代码里**（`:171,176,183,610,618,700,799,831`） |
| 9 | `agent_calls` 无保留策略 | **30 天删明细 + turn 行长留 + 日汇总** | red-team §5.1 | 写放大 7–100KB/轮；多用户 1 年 50–110GB |
| 10 | "关开关即回滚" | **两级：软回滚(env) / 硬回滚(revert)** 逐批标注 | red-team §4.2 | `LEAD_PROFILE_ENABLED` 回滚不了已删代码 |

## 1. 前提（0.5 小时，**任何批次之前**）

> **把"优化的是哪个模式"钉进仓库，而不是钉在某台机器的未入库 .env 里。**

| # | 动作 | 落点 | 验收 |
|---|---|---|---|
| P1 | `.env.example` 增 `LEAD_AGENT_MODE=single_agent`（注释：legacy 仅回滚用） | `backend/.env.example` | 新机器按示例起 → single_agent |
| P2 | 启动日志打印**生效的 mode** | `main.py` / `config.py` | 启动日志有 `lead_agent_mode=…` |
| P3 | 打包脚本读 mode，非 single_agent 直接中断 | 打包脚本（随 S4-2） | 改成 legacy → 打包失败 |
| P4 | 回滚开关表补 `LEAD_AGENT_MODE`（需重启，非热开关） | §4 | — |

## 2. 五条决策（v2 定稿）

| 决策 | verdict | 变化 |
|---|---|---|
| **D1** 能力画像 | 修改后保留 | 画像加 `review_policy`（HARD/SOFT + 豁免）；加 entry 分支棘轮。v1 把"分化"当零行为，实际是**改判据** |
| **D2** 不引入 LangGraph | 保留+补重估 | v1 论据（1.5 万行迁移）偷换概念：缺的两项自研约 **400–600 行**。补触发：①≥3 分支需合并报告 ②审批需跨进程 >1h 续跑 ③**2027-05-01 强制复查** |
| **D3** 教学版单开 | 保留 | "B4 与 2/3 并行"在单人开发下**不成立**，另算 1–2 周 |
| **D4** 工具调用日志 | 修改 | 三件齐：**读侧**（谁看/看什么/多久）+ **保留策略** + **钩子收敛**；先修 S0.5 |
| **D5** 拆 prompt | 修改 | `LEAD_AGENT_SYSTEM` 只在 single_agent 生效（legacy 早拆过，`lead_decider.py:422-426`）；画布规则占比实测 **19.8%**，v1 的"1/3"偏高 |

## 3. 批次 v2

### 3.0 压力测试：只有 N 天做 N 天的事

| 天数 | 做什么 | 第一个砍 | 理由 |
|---|---|---|---|
| **5** | S0.5(0.5) + S2a(3) + 双跑(0.75) + B1-lite(0.75) | — | 刚好一轮 |
| **4** | 同上 | **B1-lite** | 双跑不能砍：它同时决定交付模式与 prompt 是否退化 |
| **3** | S0.5 + S2a（双跑降级 10 题 smoke） | 降级双跑 | smoke 只抓崩不抓成本 |
| **2** | **只做 S2a** | 其余 | 唯一用户能直接看见的产出 |

### 3.1 S0.5 · 观测读侧（0.5 天，最先做）

**问题**：`trace.span()` 只 append 内存 `SpanRecord`，**不桥接 Langfuse**；本地模式只打 name/latency/input/output/error，**不打 metadata**（`observability.py:210-233`）→ 台账/step_stats **没有消费者**（全仓 6 处：`lead_agent.py:448`、`react_agent.py:110,438`、`agent_orchestrator.py:289,849`、`ai_service.py:487`）。

**改动（约 15 行）**：①本地日志加 `metadata=<_summarize(sp.metadata)>`；②循环里台账改 `logger.info("[lead_unified] ledger_step %d %s", step, ledger.summary())`（span 保留给 Langfuse 开时用）；③测试用 `caplog` 断言"每步一行 ledger_step"，**不是**断言 `SpanRecord.metadata` 非空（那锁的是机制，不是可观测性）。
**验收**：跑一轮对话，日志能按步看到 token 结构（Langfuse 关着也能看）。**回滚**：硬（revert）。

### 3.2 S2a · 能力画像 + 双助手分化（3 天，唯一用户可感的批次）

| # | 内容 | 落点 |
|---|---|---|
| S2a-1 | `capability.py`：`EntryProfile`（prompt 三段 / 工具子集 / 配额 / **review_policy** / 派发内核 / 产物白名单） | 新文件；映射只允许在这里 |
| S2a-2 | 拆 prompt：`LEAD_CORE` + `CHAT_PROFILE` + `CANVAS_PROFILE`（3 个 yaml + 3 个 fallback；`prompt_registry.py:79` 用 `glob("*.yaml")` 自动加载，**新增文件不用改注册表**） | `ai_prompts.py` |
| S2a-3 | chat 去掉 `get_canvas_layout`（死工具，`canvas_state.py:69-71` 恒返回空串） | `lead_agent_tools.py:89-100,136` |
| S2a-4 | 测试改造：三段各自与 yaml **逐字节**一致；**组合层用结构化断言**（CHAT 不含该工具、不含 "KPI 卡片" 小节；CANVAS 含 "落块" 小节），别用散词表 | `test_lead_p1_agent_tools.py:113-131` 等 |
| S2a-5 | **entry 分支棘轮** | 新 `tests/test_entry_isolation.py` |

**棘轮实现（对 red-team 原提案的改良——"除 2 个文件一律失败"会因 14 处存量把测试逼死）**：
- 快照键 = `(file, 函数名, 归一化行文本)`，**不用行号**（无关改动假红 → 测试被删）；
- 三条规则：新增 `entry ==/!=` → 红；快照条目消失 → 也红（防陈旧条目赖着）；`capability.py`/`lead_worker.py` 不计；
- 匹配器排除 SQLAlchemy 列比较（`AISession.entry ==`，如 `ai.py:336,338,393,2252,2269`）；
- `--update` 一键重生成，失败信息直接打印新快照。

**验收**：chat 工具表无死工具；CHAT 组合无画布小节；棘轮绿；**33 题 eval 通过率下降 ≤5%**（拆 prompt 是行为变更，不是重构）。**回滚**：软 `LEAD_PROFILE_ENABLED`（回不到已拆的 yaml），关键靠 eval + revert。

### 3.3 E0 · 双跑对拍（0.75 天，决定交付模式 + 对齐两份矛盾数据）

**要解决的矛盾**：`backend/.env:32` 的 A/B 记录是 single_agent **139 < 187**（更省，所以切默认）；`lead_agent_unified.py:56-76` 的注释是 20 题单 Agent **多 47 次（+32%）**（更贵）。方向相反、样本口径不明。
**做法**：`tests/agent_evals/dataset.jsonl`（33 题）× 两模式 → 4 张表：派发分布 / LLM 调用数 / P50-P95 时延 / degradation 分布。
**产出**：①目标模式定稿（回填 §1 P1）②成本基线（供 §5）③S2a 的行为回归基线。

### 3.4 B1-lite · 可观测垂直切片（0.75 天）／ B1 全量（5–8 天，另排期）

**lite**：①`contextvars.ContextVar("trace_id")` 在入口 set（`ai.py:667`/`:1723`）+ **`turn_id`**；②`ToolExecutor.execute_tool_call` **出入口一个钩子**（白名单/未知工具/超时都在函数内记 `error_kind`，不单列）；③`agent_calls` 表（迁移 `0023`）+ 30 天保留 + turn 行长留；④`scripts/agent_trace.py` 打时间线；⑤一条读侧 SQL 模板。

**全量追加**：`recon_tool` 钩子（保留——Lead 观察结果是"它看了什么才这么答"的唯一证据链）、`run_analysis` 收尾（**必须处理 `lead_tools.py:617-661` 提前 return 的 clear_canvas 路径**，否则漏第 7 条静默路径）、`llm_client._log_usage` 钩子（一次覆盖 5 处 usage + 流式）、错误码贯通（v1 说"全部从现有代码派生"**对工具结果路径不成立**：guard 只返回 `guard.reason`，`layer` 只在日志里）。

**关键修正**：`trace_id` 粒度是**请求**不是**轮**——HITL 确认是**第二次 POST**（`ai.py:723` 的 `body.resume`/`ui_action`），会产生新 trace_id。要回答跨确认的"这轮为什么没出图"，必须有 **`turn_id`**（或续跑复用原 trace_id + `parent` 串起来）。

### 3.5 DQ M1 · 数据体检（2–3 天，**单列批次**）

v1 把它写成"S2-4 的验收"，而它自己就是 2–3 天（`DATA_QUALITY_CLEANING_PLAN.md:12`）。它是用户点名的"对话助手要有清洗能力"的**主线**。v2 补两笔 v1 漏的成本：
1. **3 个新 SSE 事件**（`quality_report`/`cleaning_plan`/`cleaning_result`）——SSE 中继是**白名单**，漏一个就**静默消失**（`test_lead_p1_ask_kind_guard.py:139-149` 记着这坑踩过两次）；
2. **5 处前端改动**（体检卡、派生徽章、详情区块、两个 store 分支）。
顺序：S2a 先落地后，`inspect_dataset` 可直接作为 chat profile 的第一个工具接进来。

### 3.6 S3-1 / S3-2（1 天）与延后项

- **S3-1** `LEAD_AGENT_MAX_LLM_CALLS` 总预算闸（子内核有 24，Lead 层无上限，最坏 6×(1+24)）——**档位必须来自 E0 基线，不能拍**；
- **S3-2** 首步流式（现在整步 `streaming=False`，TTFT = 整步生成时间）。

**延后（带触发条件）**：S3-3 错误码策略表（触发 = `error_kind` 分布给出 top3 可修复错误；前置 = 统一错误契约）；S3-5 预算档位（触发 = E0 + S3-1）；S2b（上下文源 / translator 合并）；S2-5 constraints（现在是**假透传**：全仓只有 `lead_tools.py:610` 读 `mode`，且 memo key 不含 constraints（`:81-85`）→ 加字段会命中旧 memo 返回旧结果，**先修 memo key**）。

## 4. 验收门槛 v2（四级）

| # | 门槛 | 做法 | 适用 |
|---|---|---|---|
| 1 | 全量：**1138 collected / 0 failed**（当前 2 个 `test_chart_export` 因缺 chromium 失败 → 应改成 skip 并列名） | `backend/.venv/Scripts/python.exe -m pytest -q` | 每批 |
| 2 | eval 33 题 ≥ 基线 −5% | `tests/agent_evals/run_eval.py` | S2a 必跑 |
| 3 | 迁移往返：`upgrade head → downgrade -1 → upgrade head` | docker-compose PG | B1 起 |
| 4 | 架构守卫 + 棘轮（`test_architecture_consistency` / `test_lead_p1_agent_tools` / `test_entry_isolation`） | pytest 子集 | 每批 |

> 门禁里写死解释器：用系统 python 会有 **7 个文件收集失败**（fastapi/starlette 版本冲突），失败的恰好是安全/架构守卫——**用错解释器时它们不是红，是根本没跑**。

**回滚两级**：

| 开关 | 类型 | 能回滚 | 不能回滚 |
|---|---|---|---|
| `LEAD_AGENT_MODE=legacy` | 硬（需重启） | B0/B2/S3 的大半 | B1（两条路径都生效） |
| `AGENT_CALL_LOG=off` | 软 | 写入行 | SSE 字段变更、钩子本身 |
| `LEAD_PROFILE_ENABLED` | 软 | 取哪份 profile | 已拆 yaml、已删代码 |
| `LEAD_AGENT_MAX_LLM_CALLS=0` | 软 | S3-1 | S3-2/S3-3 |

## 5. 度量（4 个数字，改前必采）

| 指标 | 采集 | 目标 | 用在 |
|---|---|---|---|
| 单轮 LLM 调用数 | `agent_calls` | S2a 后 **≤ +10%** | S2a |
| 工具失败率 + top `error_kind` | `agent_calls` | S3 后 **−20%** | S3-3 |
| TTFT P50 | turn 行时间戳 | S3-2 后 **−30%** | S3-2 |
| eval 通过率 | `run_eval.py` | **不下降**（±5%） | 全部 |

**每月三个固定动作**（没有消费者就不做这张表——v1 最贵的漏洞）：①失败率 top3 工具 → 修或删；②memo 命中率异常低的派发 → 调缓存；③p95 时延按 kind 分解 → 定下一个瓶颈。

## 6. 不做清单（v2）

| 不做 | 理由 |
|---|---|
| LangGraph 重写编排层（2027-05-01 前） | 与 5 条不变量冲突；已补三条重估触发 |
| 两个独立 Lead 循环 | 复制四段高价值逻辑；20+ 测试；无数据支撑 |
| **S3-4 dry-plan** | 校验已在执行前做了；单表宽表 schema linking 价值≈0 |
| **S2-6 translator 合并** | 打红 2 个源码级契约测试 |
| **S2-5 constraints 扩字段** | 无人消费 + memo key 不含它 = 假功能 |
| 6 个插入点 / 在 Lead 循环塞沙箱 / 卖源码 / langchain 进主仓 | 见 01/03 文档 |

## 7. 风险登记

| 风险 | 证据 | 对策 |
|---|---|---|
| 交付包默认跑 legacy | `config.py:233`；`.env.example` 缺项 | §1 的 P1–P4（0.5 小时） |
| 两份方向相反的成本数据 | `.env:32`（139<187）vs `lead_agent_unified.py:56-76`（+32%） | E0 对拍对齐 |
| 观测层批量假调用 | 6 处 metadata 无消费者 | S0.5 先补读侧 |
| 门槛失真 | 1138 collected / 2 failed + 系统 python 7 个文件收集失败 | §4 四级门槛 |
| `_ENV_FILE` 相对路径 | `config.py:339` | 必须从 `backend/` 启动；启动日志打印 mode 兜底 |
| 旧拷贝污染核对 | `docs/deliverables/lvco-bi/` 是 7 月整仓旧拷贝 | 核对只认 `backend/`；建议移出仓库 |

## 8. 分歧与共识（Lead × red-team，9 条）

| # | 议题 | red-team | Lead | 结论 |
|---|---|---|---|---|
| 1 | S0-1 是否安全 | 安全，但"唯一截断所有者"不成立 | 核过 assembler 逐条压缩，同意 | 共识：无 bug；补"错误结果封顶" |
| 2 | S0-3 修法 | 仍是"从崩变静默丢" | 认了，补 S0.5 | 共识：先修读侧再建表 |
| 3 | 基线门槛 | 1137/2 failed | 独立复现，同意 | 共识：门槛改写 |
| 4 | 6 个插入点 | 语义 3–4 类、编辑点 5–8 | 白名单分支不该单列 | 共识：contextvar + 2 钩子 + 2 收尾 |
| 5 | recon_tool 钩子 | 初判"可选"→ **收回改判保留** | 它是 Lead 动机的唯一证据链 | 共识：保留 |
| 6 | entry 守卫 | 除 2 文件外一律失败 | 14 处存量会把测试逼死 | **改良：棘轮快照（不用行号）** |
| 7 | 批次顺序 | B1 在修用户看不见的东西 | 认了，S2a 先 | 共识：S0.5 → S2a → 双跑 → B1-lite |
| 8 | DQ M1 | 被藏在 B2 验收里 | 认了 | 共识：单列 2–3 天 |
| 9 | D2 论据 | "1.5 万行迁移"偷换概念，实为 400–600 行新增 | 认了 | 共识：补重估触发 + 6 个月复查 |

## 9. 一句话

> v1 方向没错，但它优化的是一个**只存在于某台机器未入库 `.env` 里**的模式，而验收门槛**在自己的环境里就是红的**。v2 先修这两件，再把顺序改成"用户能看见的先做"，然后才是表和闸。
