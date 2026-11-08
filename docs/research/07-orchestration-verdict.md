# 07 · 编排判决书：对五个质疑的逐条取证与结论

> 评审员：arch-research ｜ 日期：2026-11-08 ｜ 性质：**只读代码评审**（未改任何代码、未动 git、未跑测试）
>
> **证据分级**
> - **[事实·码]** 直接读代码可核，给 `file:line`
> - **[事实·数]** 从仓库内 **77 份真实运行存档**（`backend/eval_logs/agent_evals_archive/*_events.jsonl`）里抽取的计划统计，脚本口径见 §7
> - **[推断]** 基于事实的判断，标注依据与不确定度
> - **[外部]** 官方文档/公开资料，给链接
>
> **重要免责**：存档日志是历史快照（部分计划里还出现 `insight`，而该工具现在已被 `AGENT_HIDDEN_TOOLS` 隐藏），计划形态可代表 Planner 的**行为倾向**，不是当前 HEAD 的实时采样。

---

## 0. 一页判决

| # | 质疑 | 判决 | 一句话依据 |
|---|---|---|---|
| 1 | 子 agent 不能并行 | **部分成立，但"不能并行"不是主因——主因是"绝大多数计划本来就没有可并行的步"** | 代码里 `gather` 是真的（`agent_orchestrator.py:401`、`canvas_orchestrator.py:258`），但实测 **62.3% 的计划是纯链**；剩下 37.7% 的"并行"理论加速上限只有 **1.23×** |
| 2 | BI 产物是线性工作流 | **成立（对画布尤其成立）** | 画布 planner prompt **明文规定线性顺序**（"每张图表之后必须紧跟一条叙事"`canvas_planner_system.yaml:29`），实测画布计划 60.6% 纯链 |
| 3 | 图编排对不对 | **三点全部成立**：依赖失败不传播 / 环只 warning / 无层间 replan | 见 §3，三处都有明确代码位置；其中"无 replan"影响最大（计划是单点赌注） |
| 4 | 分工太细还是太粗 | **"职责分工"必要，"内核实现"重复** | prompt 两两重叠仅 3-9%（必要分工）；但 **733 行**同构 mini-ReAct 被写了三遍，且已出现 5 处参数漂移 |
| 5 | 为什么不用一个 Lead ReAct 全包 | **不该用 B；应走 C（一个执行内核 + capability profile）** | ReAct 现配 6 轮上限，**结构上做不完 8 步报告**；20 工具 schema 每轮常驻（估 ≈2.9× token） |

**打分（1-5，越高越好，权重：质量 30% / 成本 25% / 可维护 20% / 可观测 10% / 线性匹配 15%）**

| 形态 | 质量 | 成本 | 可维护 | 可观测 | 线性匹配 | **加权** |
|---|---|---|---|---|---|---|
| A 现状（Lead + 三内核） | 4 | 3 | 2 | 3 | 3 | **3.10** |
| B 单 Lead ReAct 全包（20 工具） | 2 | 2 | **5** | 3 | 4 | **3.00** |
| **C Lead + 一个 orchestrator（profile 决定产物）** | 4 | 4 | 4 | 4 | 4 | **4.00** ✅ |

**DAG 是否过度设计？**
> **作为"并行执行引擎"：是，明显过度**（实测收益上限 1.23×，实际因 barrier/同步 IO/图表串行 ≈ 1.0×，却引入了共享 session 风险与三份重复实现）。
> **作为"依赖与排序声明"：不是，且必需**（"图表必须在取数之后""叙事必须引用图表结论"是真实的偏序约束）。
> 结论：**保留 `depends_on` 语义，砍掉"层内并发执行"这套机器**。

**如果只能改一件事：抽出一个 `StepRunner`（单一 mini-ReAct 步循环），并把 db session 改为"每步一个"。**
理由：它是唯一同时命中「并行安全性 / 733 行重复 / 参数漂移」三个问题的改动，且不改变任何产品行为（prompt、工具集、事件协议全部不动）。它是走向 C 的必要第一步，也是 A→C 路径上唯一不可跳过的一步。

---

## 1. 质疑一：「我的子 agent 其实不能并行运行」

### 1.1 代码层：并发**确实写了**

| 位置 | 事实 |
|---|---|
| `agent_orchestrator.py:399-403` | `levels = _group_steps_by_level(steps)` → `for level in levels: await asyncio.gather(*(self._run_step_with_timeout(step,...)))` **[事实·码]** |
| `canvas_orchestrator.py:256-260` | 同上，画布对**全部**步骤做分层并发 **[事实·码]** |
| `_group_steps_by_level` `agent_orchestrator.py:146-171` | 按最长依赖链定级；无依赖=0 层；有环/缺依赖时兜底塞下一层 **[事实·码]** |
| `tool_executor.py:692-694`（orchestrator 内单步） / `441-443`（canvas 内单步） | 单步内多个工具调用也 `gather` **[事实·码]** |

所以"没有并发"这句话在代码层面**不成立**。真正的问题在下面 4 个"串行化因子"。

### 1.2 四个串行化因子（按实际影响排序）

**因子 1：计划本身没有宽度（最主要，占 62.3%）** —— 见 §2 实测。分层并发在纯链计划上退化为串行，`gather` 里只有一个元素。这不是 bug，是**上游输入决定的**。

**因子 2：同步 DuckDB 调用阻塞事件循环（真正让"并发"失效的代码问题）** **[事实·码]**

`execute_chart_query` 是 `async def`，但其中三处在事件循环上做**同步** DuckDB 调用（不是 `to_thread`）：

| 行 | 调用 | 性质 |
|---|---|---|
| `query_engine.py:662` | `datasource_connection.ensure_attached(...)`（同步；内部 `_attach_lock` + 可能 DETACH/ATTACH） | **DuckDB 写（catalog）→ 引擎级全局串行** |
| `query_engine.py:669` | `_ensure_datasource_ready(...)` → `duckdb_client.fetchall`（1~3 次） | 同步读，阻塞 loop |
| `query_engine.py:764` | `_get_column_types(...)` → `duckdb_client.fetchall` | 同步读，阻塞 loop |

对照：**主查询**是 `await asyncio.to_thread(duckdb_client.fetchall, ...)`（`query_engine.py:823`）——所以"重查询"不阻塞，但**每次取数前的 2-5 次元数据往返全部在事件循环上串行**。N 个并发步骤 → 这些元数据调用排队执行；一旦触发 ATTACH，还会等 `threading.Lock`（`duckdb_client.py:33`），**连整个事件循环一起卡住**。

> **推断**：这就是"看起来并发、体感串行"的最直接机制——真正并行的部分（网络 LLM 调用、主查询线程池）被前端/DB 元数据串行段夹在中间，用户看到的是"一步接一步"。

**因子 3：共享同一个 `AsyncSession`** **[事实·码] + [推断]**

```
agent_orchestrator.py:479   db_session = shared.get("db_session")      # 请求级同一个 session
canvas_orchestrator.py:346  db_session = shared.get("db_session")      # 同上
tool_executor.py:117        self.db_session = db_session               # 并发 step 各建一个 executor，但共用同一 session
tool_executor.py:239        result_str = await tool.execute(user_id=..., db_session=self.db_session, **targs)
```

- 整个请求只有**一个** session（`core/database.py:10-17` `get_db` 每请求一个），`async_session_factory()` 只在**任务边界**用到（`chat_task.py:360`、`canvas_task.py:57`），步内从不新建。
- SQLAlchemy 官方文档有专章 **"Using AsyncSession with Concurrent Tasks"**，且社区有同型事故记录（"AsyncSession Shared Across Tasks Quietly Exhausted Our Pool"）[外部]。
- **推断**：并发路径下两个 step 同时 `await session.execute()/flush()` 的后果是二者之一——① `InvalidRequestError/IllegalStateChangeError` 类报错（被 `tool_executor._execute_once` 的 `except Exception` 吞成 `{"error": "..."}`，表现为"工具偶发失败、重试后才成功"，用户感知=串行）；② 驱动层排队，**静默串行**。
- **限制**：我在仓库日志（`_logs/*.log` 仅 15 个文件）里**没有**找到该错误的实例，所以这是**风险判断，不是已取证事故**。

**因子 4：显式串行 + 层内 barrier**

- 图表步骤**从不参与并发**：`agent_orchestrator.py:377` 把 `render_chart` 从分层里剔除，`434-437` 用 `for` 串行执行；注释写明"图表步骤保持串行以稳定全局 LLM 响应消费次序"（`410`）。**[事实·码]**
- 每层是 barrier：`gather` 等最慢的一步；单步超时 30s（`config.py:135` `AGENT_STEP_TIMEOUT`）/ 画布 45s（`canvas_orchestrator.py:42`）。尾延迟被放大成"整层慢"。**[事实·码]**

### 1.3 写侧：`canvas_block_writer` 的 read-modify-write 会不会"丢块"

```
canvas_block_writer.py:73   canvas = await self.repo.get_by_id(cid, uid)   # 读全量 blocks
canvas_block_writer.py:77   blocks = list(canvas.blocks or [])
canvas_block_writer.py:90   result = await handler(blocks)                 # 追加/修改
canvas_block_writer.py:94   await self.repo.update_blocks(cid, uid, new_blocks)  # 全量覆盖
```

- **是典型的 lost-update**：两个并发 `add_chart_block` 都基于同一份 N 块快照，各自 append 后全量覆盖 → **后者覆盖前者，丢 1 块**。没有版本号、没有 `with_for_update()`、没有 per-canvas 锁。**[事实·码]**
- **但当前默认不生效**：`config.py:49` `CANVAS_BACKEND_PERSIST: bool = False`，`maybe_persist` 直接返回 `None`（`canvas_block_writer.py:228`）→ 后端根本不写库。**[事实·码]**
- 默认模式下真相源在前端：`canvas_tools.py:130-137` 注释明确"这些画布工具**不写数据库**，画布由前端落块后再自动保存（有防抖延迟），所以 `get_canvas_layout` 读到的是'上一轮'的库内快照"。**[事实·码]**
- **推断**：默认模式的并发风险不是"丢块"，而是**并发步骤各自基于旧快照决策**（`CanvasStateProvider.layout(force=True)` 读 DB，而前端防抖尚未落盘）→ 重复建同主题图表、槽位冲突。一旦打开 `CANVAS_BACKEND_PERSIST`，**丢块风险立即实体化**。

### 1.4 结论与最小改法

> **今天"不能安全并行"的准确表述是**：并发**能启动**，但（a）62% 的计划没有可并行步；（b）即使有，同步元数据 + 共享 session 会让它**退化甚至出错**；（c）写侧在灰度开关下有丢块风险。
> 所以**不是"要不要并行"的问题，是"并行在当前实现下买不到收益，还在付风险"**。

**最小改法（按性价比排序，全部为增量改动）**

| 步骤 | 改动 | 工作量 | 风险 | 收益 |
|---|---|---|---|---|
| **S1（推荐立刻做）** | 加开关 `AGENT_LEVEL_CONCURRENCY`，**默认 1**（层内串行）。改 3 行：`agent_orchestrator.py:400-403`、`canvas_orchestrator.py:257-260` 用 `Semaphore(1)` 或改为顺序 for | 0.5 天 | 极低（行为=今天的纯链路径，计划宽度>1 时反而**消除**了潜在错块） | 消除风险类；实测理论损失仅 1.23×，真实损失 ≈ 1.0 |
| **S2（要真并行时）** | 每步独立 session：`_agentic_run_step` 内 `async with async_session_factory() as db`，传给该步的 `ToolExecutor`；同时把 `query_engine` 的三处同步 DuckDB 调用包 `asyncio.to_thread`；层并发上限 2-3 | 1.5 天 | 中（要保证工具内的 flush/commit 语义不被破坏） | 拿到真实 1.2~1.4× |
| **S3（仅当开灰度）** | `CanvasRepository.get_by_id` 加 `with_for_update()`，或 `canvases` 加 `blocks_version` 乐观锁 + 重读重试 | 1 天 | 低 | 关闭丢块 |

**不做 S2 就不该开 S1 之外的并发**——现在的并发是"付了复杂度、没拿到收益"。

---

## 2. 质疑二：「产生 BI 形式目前也是线性的工作流」

### 2.1 实测数据（77 份真实计划，非推测）

数据来源：`backend/eval_logs/agent_evals_archive/*_events.jsonl`，抽取所有含 `steps[]`（带 `step_id`/`depends_on`）的计划对象，去重后 **77 份**。

| 指标 | 画布路径 | 对话路径 | 合计 |
|---|---:|---:|---:|
| 计划数 | 71 | 6 | 77 |
| 平均步数 | 4.90 | 2.33 | 4.70 |
| 平均层数（= 关键路径长度） | 3.94 | 2.17 | 3.81 |
| **纯链计划**（每层只有 1 步） | **43（60.6%）** | 5（83.3%） | **48（62.3%）** |
| **含并行分支**（某层 ≥2 步） | 28（39.4%） | 1（16.7%） | 29（37.7%） |
| 观测到的最大层宽 | 4 | 2 | 4 |
| 理论最大加速（步数/层数） | 1.46× | 1.06× | **1.23×** |

计划里出现的工具分布：`add_text_block 195`、`add_chart_block 147`、`update_chart_block 6`、`render_chart 4`、`list_fields 3`、`query_sql 3`、`list_datasources 2`、`query_engine 1`、`insight 1`。
→ 存档以**画布报告**为主；对话路径只有 6 份（复杂任务多数走了 react，或该批评估未覆盖）。

典型"有宽度"的计划长这样（`final_v5_events.jsonl Q028`，7 步 / 2 层）：

```
1 add_text_block  dep=[]        ┐
2 add_chart_block dep=[]        ├ 同层（3 个独立写入）
3 add_text_block  dep=[2]       │  ← 依赖 2，理论上应在下一层
4 add_chart_block dep=[]        ┘
5 add_text_block  dep=[4]
...
```

**关键观察**：宽度来自"多个互不依赖的**落块**"，不是因为任务天然可并行。而真正需要并行的地方（同一章节的图 + 叙事）**恰恰有依赖**。

### 2.2 代码与 prompt 层：为什么必然是链

1. **画布 planner prompt 明文规定线性顺序** **[事实·码]**
   - `canvas_planner_system.yaml:24`：「先 h1 标题 → 每个章节：**图表 → 叙事** → 章节之间可加 h2」
   - `canvas_planner_system.yaml:29`：「**每张图表之后必须紧跟一条** add_text_block(text) 叙事步骤」
   - `canvas_planner_system.yaml:37`：「叙事步骤 depends_on 它引用的图表步骤；章节标题 depends_on 前面的图表步骤（**保证顺序**）」
   → 这里的 `depends_on` 语义是"**排序**"，不是"数据依赖"。它被用来表达"先写图再写叙事"，本质上就是一条链。
2. **对话侧 planner 只有一个示例，且是单步** **[事实·码]**：`orchestrator_system.yaml:39-47` 唯一示例是 1 步、`depends_on: []`；规则 4/5 只在"需要前一步结果"时才要求连接（`26-27`）。prompt 里**从未鼓励"多个互不依赖的查询并行"**。
3. **图表步骤被代码串行** **[事实·码]**：见 §1.2 因子 4。

### 2.3 判决

> **"图编排"作为并行能力：基本是纸面能力。**
> - 62.3% 的计划根本没有宽度；有宽度的 37.7% 里，理论加速上限 1.23×，而实际被 barrier（`gather` 等最慢）、同步元数据、图表串行吃掉，**观测收益 ≈ 1.0×**。
> - 更关键：**宽度不是规划目标**，是"报告里有 3 个章节所以 3 次落块恰好无依赖"的副产物。没有一条 prompt 或代码在鼓励"并行思考"。
>
> **但 `depends_on` 本身不是纸面**：它承担"图表在取数之后、叙事在图表之后"的偏序保证，删掉会直接导致叙事引用不存在的图。**该保留的是这层语义，该删的是"分层并发执行"这套实现。**

---

## 3. 质疑三：「子 agent 的图编排是对的吗」——三点逐条

### (a) 依赖失败不传播 —— **成立**

```
agent_orchestrator.py:782-788
    prior: list[str] = []
    for dep in step.get("depends_on") or []:
        r = results.get(dep)
        if r:                                        # ← 只要"有值"就拼进去
            prior.append(f"- 步骤{dep}: {compact_result_json(r, 600)}")   # ← 截断 600 字符
```
- 上游失败/超时/跳过时，`results[dep]` 是 `{"skipped": true, "skipped_reason": "..."}` 或 `{"error": ...}` 或 `{"skipped":true,"timeout":true}`，**同样"有值"** → 被当作正常依赖结果塞进下游上下文（还截断到 600 字符）。**[事实·码]**
- `_fill_step_data`（`851-858`）同理：`if not dep_result: continue` —— 只判空，不判状态。
- **后果**：下游 Executor 看不到"上游挂了"，只能从一句被截断的 `skipped_reason` 里猜；典型表现是 ①图表拿到空 rows 被工具拦下（浪费 1-2 步重试）；②或用文字编一段"结论"（无数据支撑）。**严重度：中**（画布路径下更明显，因为叙事步骤强依赖图表）。
- **最小修法**：依赖结果里带显式状态，下游开头一行判定——`if any(dep_result.status != "ok"): mark skipped(reason="依赖步骤 N 失败") and return`。落点：`_build_executor_context` + `_run_step_with_timeout`（orchestrator 与 canvas 各一处）。

### (b) 环检测只 warning 不 fail-fast —— **成立**

```
agent_orchestrator.py:60-72  _topo_sort: 有环 → logger.warning("计划依赖存在环，忽略环中依赖") 然后继续
agent_orchestrator.py:160-164 _group_steps_by_level: 有环/缺依赖 → 兜底塞进下一层
planner_agent.py:32           PlanStep.depends_on: list[int] —— 只校验类型，不校验无环
```
- prompt 里"依赖不允许循环"（`orchestrator_system.yaml:51`）**只是劝告**，没有任何代码闸。**[事实·码]**
- **后果**：低概率（LLM 极少产出环），但**静默**——环会让层号计算失真、执行顺序不确定，表现与 (a) 同类（下游拿到错位的依赖）。**严重度：低，但"静默"这一点不可接受**。
- **最小修法**：`_plan_node` 里跑一次 Kahn，若有剩余节点 → ①先重试规划一次（把"环"作为错误回喂，沿用 `planner_agent.py:348-370` 已有的 schema-重试通道）；②仍失败 → 走 `error` 节点。**禁止静默**。

### (c) 无层间 replan —— **成立（影响最大）**

```
agent_orchestrator.py:225-246
    plan →(ok)→ execute_steps →(chart)→ chart_steps → report
                     └→ report
    # 没有任何回边到 plan
```
- 图是**单向**的：计划一旦生成，6-8 步一路执行到底（或失败跳过），**无法在中途改计划**。**[事实·码]**
- 现有补偿手段都发生在**步内**：失败签名 3 次跳过（`731-748`）、强制换工具指令（`748-755`）、hint 重试（`react_agent.py:509-521`）、全局 LLM 预算（`383-398`）。**没有任何"改计划"的通道**。
- **后果**：计划是**单点赌注**。规划错一个维度（如用户点名 3 维度只规划 2 个）→ 6 步全部执行完 → 报告缺章节；只能靠 Lead 层 `repair` 再派一次（整轮重跑，成本翻倍）。**严重度：中偏高**，与"画布报告强依赖规划质量"叠加。
- **最小修法**（成本递增）：①**执行后校验**：每层结束检查"本层产出是否覆盖了计划意图"，缺则把剩余步骤 mark skipped 并让 report 显式说明（0.5 天，不需要回边）；②**一次重规划**：`execute_steps` 后加条件边到 `plan`（限制 `replan_count<=1`），代价 +1 次 planner LLM。**推荐先做 ①**，因为 ② 会破坏"LLM 预算"模型（`_ensure_llm_budget` 按 `len(steps)` 算）。

---

## 4. 质疑四：「分工太细还是太粗」

### 4.1 工具集矩阵（20 个注册工具的精确切分）

注册表总计 **20**：`agent_tools.py` 13 个（`1912-1924`）+ `canvas_tools.py` 7 个（`CANVAS_TOOL_NAMES`，`:85`）。隐藏 6 个：`AGENT_HIDDEN_TOOLS = {insight, clean_suggest, polish_text, recommend_charts, validate_chart, data_quality}`（`agent_tools.py:53-56`）。**[事实·码]**

| 入口 → 内核 | LLM 可见工具数 | 工具集合口径 | prompt（行/字符） | 内核文件 |
|---|---:|---|---|---|
| chat → react | **7** | `20 - 7(canvas) - 6(hidden)` = `list_datasources, list_fields, query_sql, render_chart, query_engine, list_metrics, stats_analyzer`（`lead_tools.py:431-437`） | `agent_system.yaml` 122 行 / 5927 字符 | `react_agent.py` 531 行 |
| chat → orchestrator | planner **7** / executor **7** | `ToolRegistry.orchestrator_safe_tools()` = 13-6（`agent_tools.py:329-334`） | `orchestrator_system.yaml` 55 + `executor_system.yaml` 46 | `agent_orchestrator.py` 1156 行 |
| canvas → react | **12** | `CANVAS_ALLOWED_TOOL_NAMES = 7 canvas + 5 query/list`（`v1/ai.py:79-82`） | `canvas_agent_system.yaml` 39 行 | 同上 |
| canvas → canvas | planner **12** / executor **7** | executor = `extra ∩ _EXECUTOR_TOOL_NAMES`（`canvas_orchestrator.py:498-506`） | `canvas_planner_system.yaml` 63 + `canvas_executor_system.yaml` 32 | `canvas_orchestrator.py` 633 行 |
| Lead（P1 单循环） | **10** | 观察 5 + 派发 2 + 终止 3 | `lead_agent_system.yaml` | `lead_agent_unified.py` 548 行 |

### 4.2 prompt：是**必要分工**，不是重复

两两比较非空行（>12 字符）重叠：

| | 重叠行数 | 占后者比例 |
|---|---:|---:|
| `orchestrator_system` ∩ `canvas_planner_system` | 9 | 21% |
| 其余任意两两 | 1-2 | 2-9% |

→ 各 prompt 讲的是**不同的活**（规划 vs 执行 vs 画布落块），重叠主要来自"JSON 骨架格式"这类样板。**判定：分工合理。**

### 4.3 代码：是**重复实现**

三个内核各自实现了一遍"单步 mini-ReAct"：

| 文件 | 位置 | 行数 |
|---|---|---:|
| `react_agent.py` | `_reason_node 119-202` + `_execute_tools_node 250-456` | **291** |
| `agent_orchestrator.py` | `_agentic_run_step 476-770` | **295** |
| `canvas_orchestrator.py` | `_agentic_run_step 343-489` | **147** |
| | **合计同构代码** | **733 行** |

已抽出去共享的只有 `ToolExecutor`（251 行，单次工具调用生命周期）与 `Graph`（156 行），**步循环本身没有抽**。三份里逐项重复的逻辑（9 项）：

1. LLM 流式消费 + `reasoning_content` 捕获 + `observe_llm_call` span
2. `asyncio.gather` 执行本轮全部 tool_calls（都走 `ToolExecutor`）
3. `compact_result_json` 回填 tool message
4. 失败签名 `_make_fail_key` + 同签名 3 次跳过
5. 无工具调用逃逸计数 `no_tool_streak >= 2` → skip
6. 连续 API 异常 `consecutive_errors >= 3` → 放弃
7. 单步工具调用上限
8. 静态上下文拼装（用户问题 + 历史摘要 + 本步 goal + 依赖结果 + 数据源）
9. goal 达成判据

**且已经出现漂移（5 处，可验证）**：

| 参数 | react | orchestrator | canvas | 后果 |
|---|---|---|---|---|
| 循环上限（量纲不同，正是问题所在） | **整体 6 轮**（`:33`） | **单步 3 次工具**（`:449`） | **单步 4 次工具**（`:41`） | 三处无法横向比较，调参只能靠猜 |
| 单步超时 | — | **30s**（`config.py:135`） | **45s**（`:42`） | 画布更宽松，行为不一致 |
| 查询熔断 | **有**（5 次连败 / 累计 5 次 query，`react_agent.py:33-36`） | 无 | 无 | 对话页能熔断，编排器靠单步上限 |
| goal 达成判据 | `_has_tool_action`（`agent_orchestrator.py:116-135`） | 同左 | `_has_canvas_action`（`canvas_orchestrator.py:59-74`） | 两套实现，语义应统一抽象 |
| 工具白名单 | schema=可见集 | schema=可见集 | **schema 7 / `allowed_tools` 12**（`:357` vs `:504-506`） | **幻觉调用 `query_sql` 会被放行**，与模块头"执行阶段工具集再次收紧"的声明矛盾 |

### 4.4 判决

> **"分工"不细，细的是"内核数"。** 需要的分工只有三层：**决策（Lead）/ 规划（planner）/ 执行（executor）**。现在把"执行"写成了三份，其中画布版与通用版有 **80% 结构相同**（147 vs 295 行，差异只是 prompt、白名单、goal 判据、补图与自动布局两个后置钩子）。
> **保留 4.2 的 prompt 分工，合并 4.3 的代码。**

---

## 5. 质疑五：「为什么不用一个 Lead ReAct 单 agent 执行所有」

### 5.1 三形态与硬约束

**B（单 Lead ReAct 全包）在当前配置下结构上不可行**，两条硬约束 **[事实·码]**：

1. `react_agent.py:33` `MAX_ITERATIONS = 6`；一个 8 步画布报告 = 8-12 轮（每步 1-2 次工具调用）→ **必然在中途被截断**，只能靠 `_run_wrapup_report` 草草收尾。
2. `react_agent.py:36` `MAX_QUERY_CALLS = 5`：8 步报告里通常 ≥3 个查询步骤（图表步骤自带取数）→ 5 次上限很快打满，后续查询工具被动态屏蔽（`_tools_for_state`）。
3. 20 个工具 schema 每轮常驻：现有实测口径是 **7 工具 ≈ 1947 token**（`context/budgets.py:129` 注释"实测值"）→ 20 工具按线性外推 ≈ **5.5k token/轮**（**[推断]**，误差可能 ±30%）。8-12 轮 → 仅 schema 就多付 **≈ 3.5k × 10 ≈ 35k input token/请求**。
4. 无 `plan` 事件 → 前端"正在做什么"的可视化能力消失（`canvas_orchestrator.py:208`、`agent_orchestrator.py` 均 emit `plan`）。
5. 无阶段隔离会复现已踩过的坑：`canvas_orchestrator.py:11-15` 明确记录"执行步骤反复调 `query_engine` 拿数据而不调 `add_chart_block`，导致图表/叙事块缺失"——**这正是当年引入执行器白名单的原因**。

**A（现状）的代价**：三个内核（733 行重复）+ 三套参数漂移 + 并发安全面 ×3。
**C（中间态）的可行性已被现实验证**：`PlannerAgent` **本来就支持注入 prompt**（`planner_agent.py:171,259`），画布版已经在用（`canvas_orchestrator.py:110`）。

### 5.2 打分（1-5，越高越好）

| 维度 | 权重 | A 现状 | B 单 ReAct | C 单 orchestrator + profile |
|---|---:|---:|---:|---:|
| **质量**（数字可溯源/评审/规划可见） | 30% | 4（有 planner + report + Lead 评审闸） | 2（无 planner、无评审闸；长任务 context rot） | 4（评审批次不变，规划保留，仅合并内核） |
| **成本**（LLM 调用数 × 每轮 token） | 25% | 3（≈11 次调用 × 1.9k schema） | 2（次数少但每轮 5.5k schema；且需放宽轮次到 12+） | 4（同调用数，少一套 schema 预算与一次收尾调用） |
| **可维护性**（重复代码/漂移面） | 20% | 2（733 行 ×3，5 处漂移） | **5**（一个循环） | 4（删 633 行画布内核，规划/执行各一处） |
| **可观测性** | 10% | 3（三套 trace 名 / step_stats 口径不一） | 3（一套，但缺 plan 事件与阶段） | 4（一套 trace + profile 标注阶段） |
| **对"分析本质是线性流程"的匹配度** | 15% | 3（DAG 只为 1.23× 服务） | 4（本来就线性） | 4（保留 depends_on 排序，去掉并发机器） |
| **加权** | | **3.10** | **3.00** | **4.00** |

> 注：B 的"可维护性 5"是真实优势，但被"质量 2 + 成本 2"抵消——它把复杂度从**代码**转移到了**上下文**（长任务里 20 工具 + 8-12 轮，正好命中 Anthropic 的 context rot 与 Manus 的"100:1 输入输出比"教训）。

### 5.3 结论

> **不要 B。走 C。**
> C 的最小形态：`Orchestrator(profile)`，profile 只决定 5 件事——① planner prompt；② executor prompt；③ executor 工具白名单；④ goal 达成判据；⑤ 后置钩子（`_fill_missing_chart_types` / 自动 `arrange_layout`）。图结构差异（`report` 节点 vs `finish` 节点）也做成 profile 的一个字段。
> 删除 `canvas_orchestrator.py`（633 行）；保留 `canvas_planner_system.yaml` / `canvas_executor_system.yaml`（分工价值不变）。
> Lead 层的单循环与评审**完全不动**——它回答的是"要不要做、做得对不对"，和执行编排是两个问题。

### 5.4 「数据分析本质是线性流程」→ DAG 是否过度设计？

**分两半判：**

- **作为并行执行引擎：过度设计。** 实测：62.3% 纯链、平均理论加速 1.23×、图表步骤强制串行、层内 barrier、并发还要处理共享 session 与同步 DuckDB。**为一个 1.23× 的理论上限，付了三份内核与一类并发缺陷。**
- **作为依赖/排序声明：不过度，且必需。** 去掉 `depends_on` 会立刻出现"叙事引用了还不存在的图""图表用了没取数成功的 rows"。这部分成本极低（一个 layering 函数 + 一个字段），收益是正确性。

> **判决：保留 `depends_on` 与拓扑排序，删掉"按层并发执行"。** 即"图"退化为"带偏序约束的**串行**步骤列表"——这与"数据分析本质是线性流程"的判断完全一致，且能顺带消除 §1 的风险类。

---

## 6. 如果只能改一件事

### 候选清单（按"命中问题数 / 工作量"排序）

| 候选 | 命中问题 | 工作量 | 行为变化 | 评判 |
|---|---|---|---|---|
| **① 抽 `StepRunner` + 每步独立 session** | Q1（并发安全）、Q4（733 行重复）、Q5（走向 C 的必经步骤） | 3-4 天 | **零**（prompt/工具/事件协议全不动） | ✅ **选它** |
| ② 删 `CanvasOrchestrator` → profile（C 第一步） | Q4、Q5 | 2-3 周 | 中（画布补图/自动布局要迁成钩子） | 第二步 |
| ③ 层内并发默认关（S1） | Q1 风险 | 0.5 天 | 极小 | 可随 ① 一起合 |
| ④ 依赖失败传播 + 环 fail-fast | Q3(a)(b) | 1 天 | 小 | 可随 ① 一起合 |
| ⑤ 单 Lead ReAct 全包（B） | Q4/Q5"简化"诉求 | 2 周 | **大**（质量下降、需放宽轮次） | ❌ 不做 |

### 唯一推荐：①

```
新增：backend/app/services/agents/step_runner.py
  class StepRunner:
      # 参数化三处差异
      def __init__(self, *, llm, executor_factory, system_prompt, tools_provider,
                   goal_check, max_tool_calls, step_timeout, fail_limit, on_step_done)
      async def run(self, step, results, state, **shared) -> None:
          # 1) 每步独立 session（或由 executor_factory 传入）
          # 2) LLM 流式消费 + reasoning_content + observe span
          # 3) gather 本轮工具（ToolExecutor）
          # 4) compact 回填 + 失败签名 + no_tool_streak + consecutive_errors
          # 5) goal_check 判定
```

三个调用点各自收敛为：

| 调用点 | 现状行数 | 改后 | 传入的差异参数 |
|---|---:|---:|---|
| `agent_orchestrator._agentic_run_step` | 295 | ≈ 40 | prompt=`EXECUTOR_SYSTEM`，tools=orchestrator_safe，goal_check=`_has_tool_action`，max=3，timeout=30 |
| `canvas_orchestrator._agentic_run_step` | 147 | ≈ 35 | prompt=`CANVAS_EXECUTOR_SYSTEM`，tools=∩executor，goal_check=`_has_canvas_action`，max=4，timeout=45，`on_step_done`=ledger 记录 |
| `react_agent`（整循环，非步） | 291 | 保持独立（它是"无计划循环"，语义不同） | — |

**为什么它能同时解决三件事**：
1. **并发安全**：session 生命周期收在一处 → 每步一个 session（`async_session_factory()`），一次改对所有内核；
2. **重复代码**：733 → ≈ 75 行 + 一个 200 行内的 StepRunner；
3. **参数漂移**：3/4、30s/45s、白名单、goal 判据全部变成**一处可读的实参**，写单测直接断言"三份配置的差异集合 == 预期"。

**为什么它不是过度重构**：它不改任何 prompt、不改任何工具、不改事件协议、不改图结构。行为等价性可以用现有 eval 存档直接对拍（同样的 plan 集合，比较步骤结果与事件序列）。

### 附：如果"只改一件"指的是**产品级**

那就选 **②（删画布内核并入 profile）**。但请注意：**② 内部仍然需要 ①**——否则你是把 633 行复制进 profile 分支，重复换个地方住。所以顺序不可颠倒。

---

## 7. 证据附录（口径与方法）

### 7.1 计划统计口径（§2 的数字怎么来的）

- 输入：`backend/eval_logs/agent_evals_archive/*.jsonl`（26 个 `*_events.jsonl`），逐行 `JSON.parse`，递归查找满足"含 `steps` 数组且每个元素有 `step_id`"的对象。命中 **77** 个，按 `(文件, question_id, steps JSON)` 去重后仍为 77。
- 分层算法与生产一致：复刻 `_group_steps_by_level`（`agent_orchestrator.py:146-171`）的"最长依赖链定级"，`depends_on` 里不存在的 id 先过滤。
- "纯链"= 每一层的步数都 == 1；"含并行分支"= 存在某层步数 ≥ 2。
- 路径归类：任一步 `tool` 以 `add_`/`update_` 开头 → 画布路径（71）；否则对话路径（6）。
- **口径局限**：①历史快照，prompt 版本可能早于当前 yaml；②对话路径样本仅 6 份，统计意义弱；③不含"计划被 fallback 替换"的情形（`_build_fallback_plan` 产生的计划未标注，可能混入）。

### 7.2 未做/无法做的验证（诚实标注）

| 想验证的 | 为什么没做 | 替代方案 |
|---|---|---|
| 共享 `AsyncSession` 并发是否真的报错 | 需起服务/连库跑真实并发，超出"只读评审"边界；仓库日志样本不足 | 写一个 20 行的对照脚本：`asyncio.gather(session.execute(...), session.execute(...))`，观察 `InvalidRequestError`；或读 SQLAlchemy 官方 "Using AsyncSession with Concurrent Tasks" 章节 |
| 20 工具 schema 的真实 token | 需 import 应用跑 `ToolRegistry.schemas()`（会拉起 config/engine） | 现成口径 1947 token / 7 工具 线性外推；建议用一次离线脚本精确测量后替换本文的 5.5k 估算 |
| 并发是否带来真实时延收益 | 同上 | 加 `AGENT_LEVEL_CONCURRENCY` 开关，A/B 同题集跑 P50/P95 |
| 事件流是否有全局锁导致串行 | 已 grep：`llm_client.py` / `observability.py` 无 `Lock/Semaphore`；`emit` 是 `asyncio.Queue.put` | 结论：**LLM 与 emit 不是串行源**，串行源在 DB/DuckDB（§1.2） |

### 7.3 顺带发现的三处小问题（不在五个质疑内，但值得记）

1. **死变量**：`agent_orchestrator.py:382` `shared.setdefault("tool_memo_lock", asyncio.Lock())` 创建后无人使用（真正的锁在 `tool_executor.py:216` 的 per-key lock）。
2. **白名单不一致**：`canvas_orchestrator.py:357` 给 `ToolExecutor` 的 `allowed_tools` 是入口 12 个，而 schema 只暴露 7 个——与模块头"执行阶段工具集再次收紧"（`:11-15`）自相矛盾，属于**防御面比声明更宽**。
3. **`_fill_missing_chart_types` 绕过计划**：补图步骤直接 append 到 `state["ordered_steps"]`（`canvas_orchestrator.py:339`）并单步执行，不参与 replay/审计，也不计入 `_ensure_llm_budget` 的预算公式。

---

## 8. 判决摘要（给决策者的一句话版）

> 三个内核、一套 DAG、两条并发路径，**能力是真的，收益是纸面的**：
> - 实测 77 份计划里 62.3% 是链，DAG 的理论并行收益上限 1.23×；
> - 真并行的三个前提（独立 session、非阻塞 IO、无丢块写）**当前一个都不满足**；
> - 而因此付出的代价是 **733 行同构代码 + 5 处已发生的参数漂移 + 一类静默并发缺陷**。
>
> **行动**：先合 ①（StepRunner + 每步 session，零行为变化），再合 ④（依赖失败传播 / 环 fail-fast），然后按 C 删掉画布内核。**不要选 B**（单 ReAct 全包在 6 轮 / 5 查询上限下结构上跑不完 8 步报告）。

---

## 9. 来源

**本仓库代码（本次逐文件读过，行号以当前 HEAD 为准）**
- `backend/app/services/agents/{agent_orchestrator,canvas_orchestrator,react_agent,tool_executor,graph,planner_agent}.py`
- `backend/app/services/{query_engine,tool_executor→agents,canvas_tools,canvas_block_writer,agent_tools,llm_client,observability}.py`
- `backend/app/services/agents/lead/{lead_agent_unified,lead_tools,canvas_state}.py`、`backend/app/services/context/budgets.py`
- `backend/app/core/{database,duckdb_client}.py`、`backend/app/config.py`
- `backend/prompts/{orchestrator_system,canvas_planner_system,executor_system,canvas_executor_system,agent_system,canvas_agent_system}.yaml`
- `backend/eval_logs/agent_evals_archive/*_events.jsonl`（77 份真实计划）

**外部资料**
- SQLAlchemy 2.0 · Asynchronous I/O (asyncio) · "Using AsyncSession with Concurrent Tasks"：<https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks>
- 同型事故记录（AsyncSession 跨任务共享耗尽连接池）：<https://python.plainenglish.io/python-sqlalchemy-asyncsession-shared-across-tasks-quietly-exhausted-our-pool-b341940c0cf3>
- Anthropic · Building effective agents（workflow vs agent；复杂度只在必要时增加）：<https://www.anthropic.com/research/building-effective-agents>
- Anthropic · Effective context engineering for AI agents（context rot / 注意力预算）：<https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>
- Manus · Context Engineering for AI Agents（KV-cache 优先、append-only、100:1 输入输出比）：<https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus>

**声明**：本次评审**未修改任何代码文件、未执行 git 操作、未运行 pytest**；所有结论均由静态阅读 + 仓库内历史日志统计得出，已在正文逐条标注 [事实·码]/[事实·数]/[推断]/[外部]。

