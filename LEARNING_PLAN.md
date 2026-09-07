# Lvco BI 项目学习计划 · 两周精读

> 对象：你（大三 CS，基础一般，跟过 RAG 教程但无法独立复现）
> 目标：两周后能**用自己的话**讲清楚这个项目的架构、关键设计、Agent 系统原理
> 节奏：每天 1-2 小时，**看懂 > 看完**，不贪多

***

## 导读：为什么按这个顺序学？

我把项目拆成**三层**递进：

```
第一周：项目骨架（看得见摸得着的）
  ├── 入口 → 路由 → 依赖注入（请求怎么进来）
  ├── 模型 → Repository（数据怎么存）
  ├── 查询引擎 → DuckDB（数据怎么查）
  └── 认证 + 安全（怎么防）
  
第二周：Agent 核心（面试最值钱的部分）
  ├── LLM 客户端 → 工具注册（Agent 的手脚）
  ├── ReAct 循环（Agent 的大脑基础）
  ├── 图编排引擎（Agent 的骨架）
  └── 规划器 + 编排器（Agent 的高级形态）
```

每周周末有**复习日**，让你画出架构图、自己讲一遍。**两周后你就能面试时对着白板画出这个架构，讲出每个模块为什么这么设计。**

***

## 第一周：项目骨架

### Day 1 · 项目全景速览 ⭐

**目标**：在脑子里建一个"项目地图"，知道每个文件夹是干什么的

**任务**：

1. 打开 `backend/app/` 目录，看一级子目录
2. 打开 `frontend/src/` 目录，看一级子目录
3. 看 `backend/app/main.py`（入口文件，约 50 行）
4. 看 `backend/app/config.py` 的 Settings 类（约 80 行）

**你需要回答自己**（不用写下来，能说出来就行）：

- 这个项目后端用什么框架？数据库有几个？

- config.py 里为什么用一个类集中管理配置？

- main.py 的 app.include\_router 在做什么？

**面试价值**：面试开场必然问"介绍一下你的项目"，这就是你**第一段话**的素材。

***

### Day 2 · 路由层 + 依赖注入 ⭐

**目标**：理解"用户的请求怎么到达业务代码"

**任务**：

1. 读 `backend/app/api/v1/router.py`（约 30 行）
2. 读 `backend/app/api/v1/ai.py` 的前 50 行（只看路由定义和依赖注入）
3. 读 `backend/app/api/deps.py` 的 get\_current\_user 函数（约 30 行）

**核心问题**（读代码时问自己）：

- router.py 把一堆子路由聚合在一起，为什么不直接写在一个文件里？

- `Depends(get_current_user)` 这个写法在干什么？为什么能自动拿到当前用户？

- 依赖注入的好处是什么？如果不这么写，怎么拿到数据库 session？

**面试价值**：FastAPI 的依赖注入是**高频面试题**，能讲清楚 Depends 和 middleware 的区别，加分。

***

### Day 3 · 数据库模型 + Repository 模式

**目标**：理解"数据怎么存、怎么取"

**任务**：

1. 读 `backend/app/models/metric.py` 这个模型文件
2. 读 `backend/app/repositories/protocols.py`（看接口定义）
3. 读 `backend/app/repositories/unit_of_work.py`（看 UoW 模式）

**核心问题**：

- 模型里为什么用 `Mapped[str]` 而不是直接写 `str`？

- Repository 模式为什么要先定义 Protocol（接口）再实现？

- Unit of Work 解决了什么问题？为什么要有 flush 和 commit 之分？

**面试价值**：Repository + UoW 是**企业级项目标配**，能讲清楚这两个模式，面试官会觉得你有工程经验。

***

### Day 4 · 查询引擎 + DuckDB ⭐

**目标**：理解"用户的 SQL 查询是怎么执行的"

**任务**：

1. 读 `backend/app/services/query_engine.py` 的前 100 行（看核心函数签名和注释）
2. 读 `backend/app/core/duckdb_client.py`（约 80 行）
3. 读 `backend/app/schemas/query.py`（看查询配置的数据结构）

**核心问题**：

- 为什么用 DuckDB 而不是直接用 PostgreSQL 做分析查询？

- query\_engine.py 的 `_build_select` 函数在做什么？为什么需要拼接 SQL？

- duckdb\_client.py 为什么是单例？为什么需要加锁？

**面试价值**：**OLAP vs OLTP** 是架构面试的经典问题，能讲清楚为什么分析查询要用 DuckDB 而不是 PostgreSQL，体现你的架构思维。

***

### Day 5 · 认证 + 安全 + 中间件

**目标**：理解"用户登录了怎么知道是他"

**任务**：

1. 读 `backend/app/core/security.py`（约 50 行）
2. 读 `backend/app/core/middleware.py`（约 40 行）
3. 读 `backend/app/services/sql_guard.py` 的前 50 行（看核心函数）

**核心问题**：

- JWT 的 token 是存在哪里的？为什么不需要服务器端存 session？

- middleware 和 api/deps.py 里的 Depends 有什么区别？

- sql\_guard 在防止什么攻击？为什么 Agent 调用 SQL 需要额外的安全层？

**面试价值**：JWT 认证、中间件、SQL 注入防护都是**必问基础题**，能结合项目讲，比背八股文强十倍。

***

### Day 6 · 前端 + 前后端桥梁

**目标**：知道 Agent 返回的结果怎么变成你看到的图表

**任务**：

1. 读 `frontend/src/api/ai.ts`（看 SSE 流式接口）
2. 读 `frontend/src/hooks/useSSE.ts`（约 60 行）
3. 读 `frontend/src/components/charts/ChartRenderer.tsx`（约 50 行）

**核心问题**：

- SSE 和 WebSocket 有什么区别？为什么 AI 对话用 SSE 而不是 WebSocket？

- ChartRenderer 是怎么根据 chart\_type 选择不同组件渲染的？

- 后端返回的 chart option 和前端 ECharts 的 option 是什么关系？

**面试价值**：能讲清楚**SSE 流式传输**的原理，说明你理解实时通信，面试官会眼前一亮。

***

### Day 7 · 复习日 ⭐

**目标**：把第一周的东西串起来，画一张图

**任务**：

1. 拿一张白纸（或者用 excalidraw/draw\.io），画一张**完整的请求链路图**：

```
浏览器 → API 路由 → Depends 注入 → Service → Repository → DB
                                          ↓ 返回结果
                                       前端渲染 → 你看到图表
```

1. 按这个顺序，**口头讲一遍**（录音或对着镜子）：

   - "用户打开页面，前端请求数据..."

   - "请求经过 FastAPI 路由..."

   - "依赖注入帮我们拿到当前用户和数据库..."

   - "Service 层调用 Repository 查数据..."

   - "返回前端渲染成图表..."

**检验标准**：能对着白板讲 3 分钟不卡壳，就算过关。

***

## 第二周：Agent 系统攻坚

### Day 8 · Agent 系统全景 ⭐

**目标**：知道 Agent 系统有哪些模块，彼此怎么配合

**任务**：

1. 读 `backend/app/services/ai_service.py` 的 `agent_stream` 方法（约 150 行，核心方法）
2. 读 `backend/app/services/ai_prompts.py`（看系统提示词定义）
3. 打开 `backend/app/services/agents/` 目录，看 5 个文件

**核心问题**：

- `agent_stream` 为什么是 AsyncIterator？它返回什么类型的事件？

- 哪些事件是给前端渲染用的？哪些是给前端调试用的？

- agent\_stream 里有两个分支（简单任务 / 复杂任务），怎么决定的？

**面试价值**：Agent 架构是**这个项目最大的亮点**，能讲清楚"什么情况下走简单路径、什么情况下走复杂路径"，面试官会感兴趣。

***

### Day 9 · LLM 客户端 + 工具注册系统 ⭐

**目标**：理解 Agent 怎么"说话"和"动手"

**任务**：

1. 读 `backend/app/services/llm_client.py`（约 120 行）
2. 读 `backend/app/services/agent_tools.py` 的 ToolRegistry 类（约 80 行）
3. 看 ToolRegistry 中 `list_datasources` 和 `query_datasource` 两个工具的定义

**核心问题**：

- LLMClient 支持几种调用方式？complete、stream\_chat、tool\_call 有什么区别？

- ToolRegistry 的 schemas() 方法返回什么？为什么 LLM 需要这个？

- 每个工具函数都有 `__doc__` 字符串，为什么它们对 Agent 来说很重要？

**面试价值**：\*\*Function Calling（工具调用）\*\*是 Agent 面试的核心考点。能讲清楚"LLM 不会直接执行代码，它只是告诉你该调用哪个工具，参数是什么"，面试官会觉得你理解 Agent 的本质。

***

### Day 10 · ReAct 状态机（简单任务路径）

**目标**：理解"简单任务时 Agent 是怎么思考的"

**任务**：

1. 读 `backend/app/services/agents/react_agent.py`（约 120 行）
2. 读 `backend/app/services/agents/base_agent.py`（约 50 行）

**核心问题**：

- ReAct 的全称是什么？（Reason + Act，推理 + 行动）

- react\_agent.py 的 run 方法有几个步骤？每一步在做什么？

- 为什么 reaction loop 需要 max\_iterations 限制？没有会怎样？

- emit 函数是干什么的？为什么需要 asyncio.Queue？

**面试价值**：ReAct 模式是**最经典的 Agent 架构**（OpenAI 的很多论文都在用）。能讲清楚 ReAct 循环的原理，面试官会觉得你基础扎实。

***

### Day 11 · 图编排引擎 ⭐

**目标**：理解"Agent 怎么组织多步任务"

**任务**：

1. 读 `backend/app/services/agents/graph.py`（约 150 行，最难但最重要）
2. 重点看 Node、Edge、State 的定义

**核心问题**：

- 这个"图引擎"和 LangGraph 有什么关系？为什么作者要自己写一个而不是用 LangGraph？

- Node 和 Edge 分别代表什么？为什么 Edge 需要 condition 函数？

- State 在整个图执行过程中是怎么传递的？多个 Node 共享同一个 State 吗？

- 这个图引擎支持循环吗？怎么避免死循环？

**面试价值**：**自己实现轻量图编排**是这个项目最硬核的设计决策。能讲清楚"为什么不用 LangGraph 而是自己写"，面试官会觉得你有架构判断力。

***

### Day 12 · 规划器 + 编排器（复杂任务路径）⭐

**目标**：理解"复杂任务时 Agent 怎么规划执行"

**任务**：

1. 读 `backend/app/services/agents/planner_agent.py`（约 100 行）
2. 读 `backend/app/services/agents/agent_orchestrator.py`（约 150 行）

**核心问题**：

- PlannerAgent 是怎么把"用户问题"变成"工具调用计划"的？

- Orchestrator 的 execute\_task 有几个阶段？plan → execute → report 各做什么？

- 如果执行中某个工具失败了，Orchestrator 怎么处理？

- 降级机制：如果 Orchestrator 异常了，会怎样？

**面试价值**：**Plan-and-Execute 架构**是当前 Agent 的主流范式（AutoGPT、BabyAGI 都是这个思路）。能讲清楚这个设计，面试官会对你刮目相看。

***

### Day 13 · Agent 数据流串讲 ⭐

**目标**：把 Agent 系统完整串起来

**任务**：

1. 回到 `backend/app/services/ai_service.py` 的 `agent_stream` 方法，从头到尾再读一遍
2. 跟着一条用户请求走到底：

```
用户说"帮我分析上个月的销售额"
  → route_classifier 判断"复杂"
  → AgentOrchestrator.execute_task
  → PlannerAgent 动态规划
  → Executor 调用 list_datasources → query_datasource → render_chart
  → 结果通过 SSE 流回前端
  → 前端渲染出图表 + 文字
```

1. 打开 `backend/prompts/` 下的 YAML 文件，看看系统提示词怎么写

**核心问题**：

- 整个 Agent 系统有多少层安全防护？

- 上下文压缩（context\_utils.py）在什么时候触发？为什么需要它？

- 如果用户说"给我画个图"，Agent 会走哪条路？为什么？

***

### Day 14 · 面试话术 + 总结 ⭐

**目标**：把两周学到的知识转化成面试能说的话

**任务**：

1. 准备三段话（对着镜子练，录下来听）：

**第一段 · 项目介绍（30 秒）**：

> "这个项目是一个智能 BI 平台，核心是 AI Agent 驱动的数据分析。用户用自然语言问问题，Agent 系统自动判断任务复杂度：简单任务走 ReAct 循环快速响应，复杂任务走 Plan-and-Execute 编排器，动态规划工具调用步骤。后端用 FastAPI + PostgreSQL + DuckDB 双库架构，前端用 React + ECharts 渲染。我主要负责 Agent 系统的设计和实现。"

**第二段 · Agent 架构（1 分钟）**：

> "Agent 系统分三层：底层是 LLM 客户端封装，支持 OpenAI 兼容协议；中间是图编排引擎，我们自己实现的轻量版 LangGraph，支持有向图 + 条件路由 + 循环控制；顶层是 Agent 编排器，包含规划器和执行器。规划器负责把用户问题拆解成工具调用计划，执行器逐步执行并处理异常。如果编排器异常，会自动降级到单 Agent ReAct 模式，保证系统不崩溃。"

**第三段 · 关键设计决策（1 分钟）**：

> "三个关键决策：一是双库架构，PostgreSQL 存业务数据，DuckDB 做分析查询，因为 OLAP 场景下 DuckDB 比 PG 快 10-100 倍；二是自定义图引擎而不是用 LangGraph，因为 LangGraph 太重、依赖多，我们的场景只需要基础的有向图 + 条件路由，轻量实现更可控；三是安全层层递进，SQL 守卫 + 工具白名单 + 公式校验器，三层防护防止恶意查询。"

1. 拿出 Day 7 画的架构图，再画一遍，这次加上 Agent 系统

**检验标准**：三段话各讲 3 遍不卡壳，就能去面试了。

***

## 学习建议

### 怎么读源码？（重要）

不要从头读到尾。**三层阅读法**：

```
第一层：看文件头 → 函数签名 → 注释
  理解"这个文件是干什么的"（5 分钟）
  
第二层：看核心函数 → 数据流向
  理解"核心逻辑是什么"（10 分钟）

第三层：看细节 → 异常处理 → 边界情况
  理解"为什么这么写"（15 分钟）
```

**每天只读 2-3 个文件，读透一个比读十个强。**

### 遇到不懂的怎么办？

1. 先看有没有注释（这个项目注释很全）
2. 看函数名猜意思（Python 函数名一般是自解释的）
3. 看 import 来源（理清依赖关系）
4. 再不懂就问我

### 面试准备

**两周后你应该能回答**：

- 这个项目的技术栈是什么？为什么选这些技术？

- Agent 系统有几种工作模式？各自适用什么场景？

- 为什么用 DuckDB 而不是纯 PostgreSQL？

- 为什么自己写图编排引擎而不是用 LangGraph？

- 如何保证 Agent 的 SQL 查询安全？

- 指标治理的"血缘"和"影响分析"是怎么实现的？

- 画布和仪表板有什么区别？Agent 怎么操作画布？

***

## 学习进度追踪

每天学完后，回答三个问题（写在手机备忘录或记事本）：

1. 今天学了什么？（一句话总结）
2. 哪个概念最让我困惑？
3. 如果我明天要面试，我能讲清楚今天的内容吗？

***

> 最后一句：**两周后你不可能记住所有代码，但你能讲清楚所有设计决策。面试官不关心你背了多少行代码，只关心你理解了多少为什么。**

***

# 专项篇：Agent 系统七天攻坚 ⭐⭐⭐

> 前提：第一周的内容可以边学边补。这个专项只攻 Agent。
> 目标：**简历上敢写、面试时被追问三层还能答出来。**
> 用法：每天对我说"开始 Agent Day X"，我带你读代码 + 提问检验。

## 为什么这么排？

Agent 系统的本质是一句话：**LLM 负责想，工具负责干，框架负责把"想→干→看结果→再想"转成一个循环。**

七天就是按这句话拆的：

```
Day 1  全景：请求进来怎么分流（简单/复杂）
Day 2  嘴和手：LLM 客户端 + Function Calling 协议
Day 3  大脑：ReAct 循环 + ToolExecutor 执行内核（你重构的）
Day 4  骨架：自研图引擎（mini-LangGraph，最硬核）
Day 5  高级形态：Planner + Orchestrator（Plan-and-Execute）
Day 6  护城河：SQL 三层安全 + 压缩记忆回流（你刚做的）
Day 7  面试串讲：数据流走查 + 追问演练
```

## 每日安排

### Agent Day 1 · 全景与分流

- 文件：`api/v1/ai.py`（chat/canvas 两条路由）→ `ai_service.py` 的 `agent_stream`

- 核心：为什么需要 route\_classifier？简单任务直接 ReAct，复杂任务进编排器

- 检验：画出"用户一句话 → SSE 事件流回前端"的完整链路图

### Agent Day 2 · LLM 客户端与 Function Calling

- 文件：`llm_client.py`、`agent_tools.py`（ToolRegistry）

- 核心：LLM 不执行任何东西，它只输出"我想调哪个工具+参数 JSON"；ToolRegistry 把工具的 docstring 变成 schema 喂给 LLM

- 检验：说清 complete / stream\_chat / stream\_chat\_with\_tools 三种调法的区别

### Agent Day 3 · ReAct 循环 + ToolExecutor ⭐

- 文件：`agents/react_agent.py`、`agents/tool_executor.py`

- 核心：ReAct = Reason + Act 交替；ToolExecutor 统一"解析参数→执行→观测→emit 事件→错误判定"生命周期，orchestrator 和 react\_agent 共享它（这是你亲手重构的，面试必讲）

- 检验：解释为什么抽 ToolExecutor（消除两份重复代码 / 单点改执行策略）

### Agent Day 4 · 自研图引擎 ⭐⭐

- 文件：`agents/graph.py`

- 核心：Node + Edge + 共享 State + 条件路由；为什么不用 LangGraph（依赖重、场景简单、可控性）

- 检验：说清图引擎怎么防死循环（max\_iterations / visited）

- 配套：**LangGraph 对比认知**（见下方独立章节）

### Agent Day 4+ · LangGraph 对比认知 ⭐（不重写项目，只补认知）

> 目的：让"为什么不用 LangGraph"的故事站得住 + 简历双写（自研亮点 + 框架关键词）
> 铁律：**不改造现有项目**，只做认知对比 + 一个能跑的 LangGraph mini demo

- 任务 1：读 LangGraph 官方文档核心概念（State / Node / Edge / conditional\_edges / 循环）

- 任务 2：做一张**对比表**：你的 graph.py 引擎 vs LangGraph 各自怎么解决「状态传递 / 条件路由 / 循环终止」

- 任务 3：写一个 **LangGraph 版 mini-ReAct demo**（约 50 行，能跑）——证明"既会手写，也会用框架"

- 任务 4：产出面试标准答案："如果让你用 LangGraph 重构，会怎么设计？"（State 怎么建模、工具节点怎么抽象、循环上限放哪）

- 检验：能答出 LangGraph 的 State 和你的 State 的核心区别；能说出什么场景该用框架、什么场景该手写

### Agent Day 5 · Plan-and-Execute 编排器

- 文件：`agents/planner_agent.py`、`agents/agent_orchestrator.py`

- 核心：Planner 出计划（steps + depends\_on）→ 按依赖分层并行执行 → 单步失败≤3 重试 → 幂等工具 memo → 汇总报告；编排器崩溃降级到 ReAct

- 检验：讲清"步骤分层并行"是怎么用 depends\_on 分组的

### Agent Day 6 · 安全与记忆 ⭐（全是你的近期产出）

- 文件：`sql_guard_ast.py`、`context_utils.py`、`models/ai_memory.py`、路由里 `_load_memory_summary/_save_memory`

- 核心：SQL 三层校验（格式 → 表归属白名单 AST → 注入防护）；压缩记忆"写→存→回流"闭环（ai\_memories 表 + COMPRESSION\_MARKER 链式累积）

- 检验：解释"为什么压缩摘要要带标记注入"（下一轮能接着数轮次继续累积）

### Agent Day 7 · 面试串讲

- 跟一条真实请求走完全程，录音讲 5 分钟

- 追问演练：我扮演面试官连问三层"为什么"

***

## 改造任务 · 亲手实现"同 schema 多表 JOIN" ⭐⭐⭐（不 vibecoding）

> 触发时机：**Day 2-4 吃透之后再动手**（顺序错误会两头不到岸）。
> 铁律：**不 vibe**——每一行代码都由你亲手敲，我负责带设计、讲原理、纠偏、验测试。改出来的东西你要能讲透每一处为什么。
> 简历一句话：*"为 BI 平台设计并实现同 schema 多表 JOIN，在保持 schema 级安全白名单不变的前提下补齐跨表关联能力。"*

### 为什么要做（面试叙事）

- 主动发现缺口：一源一表 + 不做跨表是继承来的设计，跨表关联是 BI 刚需

- 亲手落地 = 从"项目自带"变成"我主导的决策"，这是简历差异化武器

- 安全红利：白名单本是 schema 级（sql\_guard\_ast.py 归属校验只锁 schema 不锁表），放开同 schema JOIN **安全边界零改动**——这个你亲手验证过的点，面试讲出来极有说服力

### 改造范围（四层都要动，动一处讲一处）

1. **schema\_meta**：数据源注册时记录该 schema 下可用表列表（`tables: [...]`），不再只有单表 `table_name`
2. **list\_datasources 工具**：返回值增加 `tables` 字段，让 LLM 知道有哪些表可 join
3. **query\_datasource 工具**：FROM 不再写死单表，允许在绑定 schema 内多表 join；错误 hint 带"可用表列表"供 LLM 自纠错
4. **sql\_guard\_ast**：归属白名单**保持 schema 级不变**（这是本轮的红线），只核对多表时每个表引用都属于绑定 schema
5. **测试**：新增 join 场景用例 + 越界 schema 仍被拦截的回归用例（证明安全没破）

### 验收标准（讲得出来才算完成）

- [ ] 能构造一条同 schema 双表 JOIN 的查询并正确执行

- [ ] 能解释"为什么白名单不用改"（schema 级 vs 表级）

- [ ] 能说出同名列冲突 / 表名限定符等实现细节怎么处理

- [ ] 回归测试证明跨 schema 访问仍被拦截

- [ ] 面试 60 秒：讲清"原设计 → 缺口 → 我做的改造 → 安全边界怎么守住"

***

## 改造任务 2 · 亲手实现"压缩记忆回流" ⭐⭐⭐（不 vibecoding）

> 触发时机：**Day 6 之后**（先学透概念，再动手）。
> ⚠️ 背景澄清：这个功能**代码已在项目里**（此前由助手代写：ai\_memories 表 + 迁移 + repository + 路由读/写），但**不是你自己敲的**。面试只能 claim 自己做过的事——所以你要把它当"没实现过"，亲手重写一遍，做到能讲透每一处。
> 铁律：**不 vibe**——每一行都由你亲手敲，我带你走完整设计 + 实现 + 测试。
> 简历一句话：*"设计并实现会话级压缩记忆：每轮结束把历史压缩成摘要持久化，下一轮开始重新注入，用标记实现跨轮链式累积，防止上下文爆炸。"*

### 为什么要做（面试叙事）

- 你亲手实现了"记忆"这个 Agent 核心能力（感知-记忆-思考里的记忆层），不是借用现成库

- 它能回答高频追问："Agent 上下文越来越长怎么办？"→ 压缩 + 持久化 + 回流

- 和 JOIN 改造一样，是从"项目自带"变成"我主导的设计"

### 改造范围（五层，动一处讲一处）

1. **AIMemory 模型**（`models/ai_memory.py`）：先删掉自己重建，讲清为什么 session\_id 唯一约束 = upsert 语义
2. **迁移**（alembic 0018）：讲清为什么建表归 alembic 管而不是 init.sql
3. **repository**（`SQLAlchemyAIMemoryRepository`）：get\_by\_session / upsert 两个方法，为什么 upsert 要先查再改
4. **ai\_service.agent\_stream**：`memory_summary` 注入点为什么放在 system 之后、以 COMPRESSION\_MARKER 标记开头
5. **路由读/写**（`_load_memory_summary` / `_save_memory`）：为什么写在事件循环里消费 `compressed_history` 事件；为什么 `_save_memory` 单独 commit
6. **测试**：新增跨轮链式累积测试（旧摘要保留 + 新摘要追加）

### 验收标准（讲得出来才算完成）

- [ ] 能画出"写 → 存 → 回流"完整链路，指出每一环在哪个文件

- [ ] 能解释"为什么压缩摘要要带 COMPRESSION\_MARKER 注入"（下一轮 `_count_rounds_since_marker` 才能接着数轮次，链式累积）

- [ ] 能说清 `_save_memory` 为什么单独 commit（防 assistant 空消息导致 rollback）

- [ ] 测试证明：注入旧摘要后新一轮压缩会"旧保留 + 新追加"

- [ ] 面试 60 秒：讲清"为什么 Agent 需要压缩记忆 → 写→存→回流怎么实现 → 标记链式累积解决了什么"

***

## 学习节奏升级：概念 → 小 demo → 笔记（铁律）

> 你反馈的核心问题：一直在读、很少实操，学过就忘，没有笔记。
> 对策：**从今天起，每个概念必须配一个 10-30 行的小 demo + 一条笔记**，否则不算学会。

### 三步闭环（每次学习都走）

1. **我讲概念**（大白话 + 类比 + 代码引用）
2. **你手写小 demo**（我在计划里指定，你敲出来、跑起来、看到结果）
3. **记笔记**（用你自己的话，一句话本质 + 一个类比 + 踩坑点，写进学习笔记文件）

### 已安排的手写 demo 清单（持续补充）

- [x] 线程锁 demo：`counter += 1` 竞态 vs `threading.Lock`（对应 DuckDB `_conn_lock`）；用 switchinterval 强制切换才触发竞争

- [x] SSE 流式模拟 demo：手写 data: 行解析 + tool\_calls 碎片 += 拼接；踩过"多转义引号→json.loads 现形"的坑

- [x] memo 并发去重 demo：asyncio.Lock + 缓存，相同参数并发只执行一次

- [x] execute\_tool\_call 迷你版：解析→查注册→memo→执行→emit→返回结果完整流程；抓到"tool(函数) vs tname(名字)"经典 bug

- [ ] mini-ReAct 轮子：LLM 调用 + 2 个工具 + 循环 + 终止条件（约 150 行，Day 4 后）

- [ ] 同 schema 多表 JOIN 改造（改造任务，亲手实现）

- [ ] 压缩记忆回流改造（改造任务 2，亲手实现）

### 复习机制（对抗遗忘）

- 每次开始新一节前，先花 3 分钟**默写**上一节的一句话本质（写不出 = 要回炉）

- 每学完一个 Day，把当天笔记贴给我，我帮你检查"是不是真懂了"（能答追问才算）

***

## 面试防身三句话（背下来）

1. **讲架构**："双路径设计：route\_classifier 判复杂度，简单任务 ReAct 快速响应，复杂任务 Plan-and-Execute 编排，编排器异常自动降级 ReAct，保证可用性。"
2. **讲重构**："我发现两个 Agent 各写了一份工具执行逻辑，抽了共享 ToolExecutor 统一'解析→执行→观测→错误判定'生命周期，一处改全局生效。"
3. **讲安全**："Agent 生成的 SQL 过三层守卫：格式校验、基于 SQLGlot AST 的表归属白名单（防跨 schema 访问）、注入特征检测，失败信息脱敏后才回给 LLM。"

## 学习方法（针对 vibecoding 项目）

vibe 出来的项目最大的坑是"代码认识你，你不认识代码"。对策：

1. **只学你自己改过/写过的部分**（ToolExecutor、sql\_guard\_ast、ai\_memories）——这些你能讲最深的"为什么"，是面试的差异化武器
2. **每个模块用"一句话本质 + 一张图"消化**，不背代码
3. **每学完一个，来找我做"面试官追问演练"**，答不上来的就是第二天要补的

