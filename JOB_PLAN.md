# Lvco BI → 秋招主力项目 · 执行计划（8 周）

> 对象：本人，大三 CS，投 **Agent 应用开发 / LLM 应用** 方向，秋招 10-11 月投递
> 时间：2026-08-25 起，每天 3-4h，周末全天
> 核心矛盾：项目是 vibecoding 写的，所有权是我的，但**没有真正被我掌握** → 本计划的目标是"所有权回收 + 增量证明"

---

## 一、定位（写进脑子的第一件事）

**一句话 pitch：**
> AI 驱动的数据分析平台：用户用自然语言提问，Agent 自动判断复杂度——简单任务走 ReAct 状态机，复杂任务走 Plan-and-Execute 编排器；所有查询经三层安全防护，结果通过 SSE 流式回传文字与图表。

**简历亮点词：** ReAct · Function Calling · Plan-and-Execute · 轻量图编排引擎 · SSE 流式 · DuckDB+PostgreSQL 双库 · 三层 SQL 安全 · 降级容错 · 指标血缘

**两条铁律：**
1. **看懂 ≠ 简历资产。"我能改变它"才是。** 每个模块读完，必须能闭卷重写函数骨架、能回答"为什么这么写"。
2. **对 vibecoding 要坦诚但有力。** 面试被问"项目是不是 AI 写的"时，大方承认使用 AI 辅助，但强调：*设计决策、模块划分、安全防护、排错验证是我完成的*——然后用 W3-W5 的增量来当证据。

---

## 二、前置工作（本周内，约半天）

先把项目真正跑起来，告别"只看代码"：

- [ ] `docker compose up` 起 backend + frontend + PostgreSQL + DuckDB + Redis + MinIO
- [ ] 注册账号；上传 `backend/mock_data/` 里一个 CSV；让它自动列出数据源
- [ ] 问一句"帮我分析上个月销售额"，亲眼看 SSE 流式响应（Network 里看 `data:` 逐条返回）
- [ ] 把登录后的 `Authorization` token 复制出来，用 curl 手打一次：
      `POST /api/v1/ai/chat/stream`，看后端日志打出 `agent_stream` 各阶段
- [ ] 建一个学习追踪文件（直接用 LEARNING_PLAN.md 末尾的每日三问模板）

---

## 三、八周路线

### W1 · 骨架回收（对应 LEARNING_PLAN Day1-7）

目标：把"请求怎么进来、数据怎么存、SQL 怎么查、怎么防攻击"变成自己的话。

| 天 | 模块 | 关键文件 | 所有权回收动作 |
|---|---|---|---|
| D1 | 全景 | main.py / config.py | 闭卷讲：include_router 在干嘛、为什么 Settings 单例 |
| D2 | 路由+注入 | router.py / deps.py / core/database.py | 闭卷重写 get_db 的 yield 骨架；讲 Depends 递归求值 |
| D3 | 模型+仓库 | models/metric.py / repositories/* | 讲 Mapped、Protocol 抽象、UoW 的 flush/commit |
| D4 | 查询引擎 | query_engine.py / duckdb_client.py | 讲 OLAP vs OLTP；单例为何加锁 |
| D5 | 安全中间件 | security.py / middleware.py / sql_guard.py | 讲 JWT 无状态、middleware vs Depends |
| D6 | 前端桥梁 | charts/ChartRenderer.tsx（仅看） | 讲 SSE 事件协议、chart option 结构 |
| D7 | 复习日 | 白板画请求链路图 | 3 分钟不卡壳 |

**周检：** 对着白板讲 5 分钟 + 现场改一行代码（比如改 JWT 过期时间，看是否全项目生效）。

### W2 · Agent 攻坚（对应 LEARNING_PLAN Day8-14）

目标：把 Agent 系统的每个"关节"拆开再装回去。

- [ ] `agent_stream` 全流程再读通（现在已具备一半基础：SSE、事件协议、分流、降级）
- [ ] **ReAct 循环**：`agents/react_agent.py` 的 run——reason → act → observe，为何要 max_iterations
- [ ] **图引擎**：`agents/graph.py`——Node/Edge/State，为什么自写而不是 LangGraph
- [ ] **规划-执行**：`planner_agent.py` + `agent_orchestrator.py`——失败处理、降级
- [ ] **工具注册**：`agent_tools.py` 的 ToolRegistry——schemas() 与函数 __doc__ 的角色
- [ ] **LLM 客户端**：`llm_client.py`——complete / stream_chat / tool_call 三种方式
- [ ] **上下文与安全**：context_utils.py、sql_guard_ast.py、prompts/ 下的 YAML

**周检：** 画一张完整 Agent 数据流图（用户提问 → 分类 → 规划 → 工具 → SSE → 前端），讲 10 分钟。

### W3-W5 · 增量 = 证明"我能独立做" ★（简历的命）

**选型（W3 第 1 天定，推荐 A）：**

- **路线 A（推荐）换场景复刻**：用 LvcoBI 的架构，从零做一个 **mini 版"游戏 Mod 社区数据分析助手"**——你懂 Steam 创意工坊/Mod 生态（数据可用公开 API 抓取或 mock），面试能讲场景故事 + 架构能力。
- **路线 B（保底）加功能**：直接在 LvcoBI 上加 feature（如指标血缘可视化、新图表类型、模型路由代理），合并回主项目。

**mini 版范围（A 路线）**，三次迭代：

| 周 | 目标 | 交付 |
|---|---|---|
| W3 | 骨架：FastAPI + LLM 客户端 + Function Calling 打通 | 能"问一句话返回一段文本" |
| W4 | 加 ReAct 循环 + 2 个工具（如 list_items / analyze_trend）+ SSE 流式 | 能"问 → 查 → 答"，前端逐字渲染 |
| W5 | 打磨：安全层（输入检查）+ 超时/降级 + 简单前端 + 3 个测试 | 干净的可演示 demo，README 写清设计决策 |

**任何"我写的"都要能回答：为什么这么设计？对比别的方案呢？**——这就是面试素材。

### W6 · 简历（2-3 天）

**项目结构：一主两辅**

```
主力：AI Agent 数据分析平台（LvcoBI，写"我动手的增量")
辅助：RAG 知识库（训练营）
辅助：数据分析项目（经验1/2）
```

**项目块写法（STAR + 数量 + 关键词）：**
> **AI Agent 数据分析平台**（FastAPI · React · DuckDB) · 核心贡献
> - 设计并实现 **Agent 双路径架构**：ReAct 状态机（简单任务）与 Plan-and-Execute 编排器（复杂任务），引入**降级兜底**，LLM 异常时自动回退，系统可用性显著提升
> - 基于自有极简**图引擎**（Node/Edge/State + 条件路由）替代 LangGraph，去除重型依赖，循环可控
> - 实现 **SSE 流式**问答（事件协议 + StreamingResponse），前端逐字渲染图表与文字
> - 构建**三层安全**：SQL 守卫 → 工具白名单 → 公式校验；配合 JWT + Redis 限流
> - 采用 **PostgreSQL + DuckDB 双库**：OLTP/OLAP 分离，分析查询由 DuckDB 承担

**三段自我介绍**：30 秒项目介绍 / 1 分钟 Agent 架构 / 1 分钟关键决策（背后真实代码为准）。

### W7 · 面试备战（1 周）

- 用下方"面试题库"逐题练，每题都能**指到具体文件 + 讲清为什么**
- 模拟面试至少 3 次（录音，听卡壳点）
- 白板练习：画架构图 + Agent 数据流图

### W8 · 投递 + 复盘

- 投递节奏：提前批 → 正式批 → 内推；简历按 JD 微调关键词
- 每面完 30 分钟复盘：问了什么 / 我哪里卡 / 项目被追问了哪一行 → 立即补进项目与话术

---

## 四、面试题库（附证据位置）

| # | 题 | 必答点 | 证据文件 |
|---|---|---|---|
| 1 | 介绍一下项目 | 一句话 pitch + 架构分层 | main.py / router.py |
| 2 | 依赖注入怎么工作 | Depends 递归、yield 事务、控制反转 | deps.py / core/database.py |
| 3 | 为什么双库 | OLAP vs OLTP，DuckDB 列存/向量化 | config.py / duckdb_client.py |
| 4 | Agent 有几种模式 | 简单→ReAct；复杂→编排器；何时分流；异常降级 | ai_service.py agent_stream |
| 5 | ReAct 循环是什么 | Reason+Act，max_iterations 防死循环 | agents/react_agent.py |
| 6 | 为什么自写图引擎 | LangGraph 重、场景只需要有向图+条件路由+循环控制 | agents/graph.py |
| 7 | Function Calling 原理 | LLM 只返回调用意图，不执行代码；schemas()+__doc__ 是关键 | llm_client.py / agent_tools.py |
| 8 | SSE vs WebSocket | 单向、HTTP、自动重连；聊天是服务端→客户端，够用 | ai.py / frontend useSSE.ts |
| 9 | 三层安全防什么 | SQL 注入、工具越权、非预期图表公式 | sql_guard.py / sql_guard_ast.py |
| 10 | 上下文超长怎么办 | 压缩策略：保留 N 轮 + 摘要折叠，字符上限兜底 | context_utils.py |
| 11 | 输入校验/限流 | JWT 无状态、Redis 限流、中间件 | core/security.py / limiter.py |
| 12 | 指标血缘/影响分析 | 血缘图与影响传播怎么建模 | models/metric_lineage.py / metric_audit.py |
| 13 | 画布与仪表板区别 | 自由布局 vs 参数化刷新 | canvas_service / dashboard_service |
| 14 | 可观测性 | Langfuse trace、超时控制、操作日志 | observability.py / settings 超时项 |

---

## 五、每日三问（沿用 LEARNING_PLAN）

1. 今天学了什么？（一句话）
2. 哪个概念最让我困惑？
3. 如果我明天面试这个模块，我能讲清楚、还能改它吗？（答案必须：能）

---

> 记住：面试官不关心代码是不是 AI 写的，只关心**你能不能讲清、能不能改、能不能现场排错**。W3-W5 那一行行"我亲手写的"代码，就是你最重要的回答。