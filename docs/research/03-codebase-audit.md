# 03 · 代码级审计：工具链路可观测性 / LangChain·LangGraph 必要性 / 对话-画布解耦成本

> 审计对象：`E:\BI\LvcoBI\lvco-bi`（backend = FastAPI + SQLAlchemy async + DuckDB；frontend = React + TS）
> 审计快照：`master @ a4aa444`（2026-11-02）
> 审计方式：**只读**（未改任何代码、未跑全量 pytest、未重启服务）。所有断言给出 `file:line`；无法从代码直接证实的标注「未确认」。
> 路径约定：下文所有路径相对仓库根。

---

## 一页结论

**A. 工具调用可观测性 —— 骨架齐、落地零。**
三层抽象都已经写好了：`observability.py` 有 Trace/Span 记录（`observability.py:75-109`）、`ToolExecutor` 在**唯一**位置统一执行工具（`tool_executor.py:152-203`）、SSE 有 `tool_call`/`tool_result` 事件（`tool_executor.py:162,189`）、`run_analysis` 还收集了内存版工具链（`lead_tools.py:722,788,981`）。
但**默认部署下没有任何一次工具调用会被留存**：Langfuse 默认关闭（`config.py:119`、`.env:30`），日志只进 stdout 无文件落盘（`main.py:15`），Redis 事件日志 TTL 1 小时 + maxlen 2000（`event_stream.py:44,55`）且每轮开始被清空（`ai.py:1020`），DB 里只存 assistant 正文（`chat_task.py:493-497`；`canvas_task.py:167-171`），`operation_logs` 只到 HTTP 粒度（`middleware.py:82-96`）。缺 trace_id、缺耗时、缺失败分类、缺 token、缺重试、缺可查询性。
**最小改动方案**：不动编排层，在 `ToolExecutor` 这一个 choke point 采集 + 1 张 `tool_calls` 表 + 复用现有「缓冲+单写入者」模式异步 flush（`chat_task.py:325-342` 已有现成范式），插入点 6 个、工作量 S/M。**收益**：一轮对话从「只有一个最终答案」变成「一条可按 trace_id 排序的完整时间线」。

**B. LangChain / LangGraph —— 企业版不引入，教学版单开 demo。**
企业版：编排层**不建议**引入。本仓已自研 156 行图引擎并显式对齐 LangGraph 概念（`graph.py:1-18`），LLM 四种调用形态（流式/非流式 × 有无 tools）都已实现（`llm_client.py:97,154,209,304`），上下文装配有 token 台账与「只在一处截断」铁律（`budgets.py:1-18`、`compress.py:1-10`），HITL 确认链路端到端可用（`lead_agent.py:632-637,839-879` + `canvasAssistantStore.ts:285-294`）。真正的短板是「看得见」而不是「能编排」——所以推荐形态 (b)：**只把 Langfuse/OTel 旁路打开并补齐脱敏与 usage**（Langfuse 已是依赖 `requirements.txt:67`，只差开关与补数据），外加形态 (a) 借鉴 LangGraph 的 interrupt/checkpoint 概念继续演进自己的 `graph.py`。形态 (c)（用 LangGraph 重写编排层）成本 L 且会与现有四道门不变量（`lead_agent_tools.py:25-31`、`test_architecture_consistency.py:53-131`）正面冲突。
教学版：学生要的是「框架用法」（就业信号），但**不要放进主仓**——会拖入 langchain 依赖并让「工具四道门」一致性测试失效；建议独立 `teaching/` 子项目（或独立仓库），用同一业务场景做「裸 SDK / LangChain / LangGraph」三实现对照，把「框架替你做了什么」讲清楚。

**C. 对话 / 画布解耦 —— 耦合面比预期小且已分化过半，推荐「能力画像」，不推荐「两个 Lead 循环」。**
提示词已按入口分化且被测试锁住（`ai_prompts.py:704`、`test_architecture_consistency.py:171-198`），工具注册表单例共享（`agent_tools.py:313-334`），上下文装配唯一出口（`provider.py:53-140`），前端本来就是两套 store（`aiChatStore.ts` / `canvasAssistantStore.ts`）。真正重复的是三处：**画布事件翻译器有三份**（`chat_task.py` 的对话版、`canvas_task.py:73-156` 的队列版、`ai.py:2020-2094` 的进程内版）、**LeadAgent 构造有 4 处**（`ai.py:1004`、`ai.py:2013`、`worker.py:144`、`worker.py:207`）、**入口差异靠字符串 `entry` 散落 if**（`lead_agent.py:523`、`lead_tools.py:610,831`、`canvas_orchestrator.py:44` 等）。按「能力画像」解耦：S/M 为主，1 处 M-L；按「两个独立 Lead 循环」解耦：L，且要动 20+ 个测试文件。

---

## A. 工具调用（toolchain）可观测性缺口

### A.0 一次工具调用的完整链路（现状）

```
POST /ai/chat/stream (ai.py:667) 或 POST /ai/canvas/chat (ai.py:1723)
  ├─ 抢会话锁 session_lock.py:26-45            → Redis key ai:session:lock:*
  ├─ 写任务态 ai.py:817-828 / ai.py:2370-2377  → Redis key ai:chat:task:* / ai:canvas:task:*
  ├─ 起任务 run_chat_task (chat_task.py:192) / run_canvas_task (canvas_task.py:37)
  │    或 ARQ 队列 worker.py:90-97 → 100 / 159
  └─ LeadAgent.stream (lead_agent.py:453)
       ├─ observer.trace("lead_agent_turn") lead_agent.py:482-488
       ├─ decide_action* (lead_decider.py:287 / 399) → LLM span
       ├─ run_worker → dispatch_analysis (lead_dispatch.py:36-88)
       │    └─ run_analysis (lead_tools.py:566)
       │         ├─ span "run_analysis" (chain) lead_tools.py:704-712
       │         └─ ReactGraphAgent / CanvasOrchestrator / AgentOrchestrator
       │              └─ ToolExecutor.execute_tool_call (tool_executor.py:152)
       │                   ├─ emit tool_call   tool_executor.py:162
       │                   ├─ observe_tool_call span（仅 trace 非 None）tool_executor.py:236-250
       │                   └─ emit tool_result tool_executor.py:189
       └─ events → chat_task._publish (chat_task.py:344-348) → event_stream.publish (Redis Stream)
```

### A.1 现在一次工具调用到底留下了什么：逐层清单

| 层 | 留下什么 | 证据 | 默认部署下可用性 |
|---|---|---|---|
| **DB 表** | 只有 `ai_messages.content`（assistant 最终正文）+ `chart_data`。**没有任何工具调用记录** | `chat_task.py:491-497`；`canvas_task.py:167-171`；表清单 `models/*.py` 共 21 张表，无 tool 表 | 有效，但粒度=轮 |
| **DB 表（审计）** | `operation_logs`：method/path/status/duration_ms/ip/ua | `middleware.py:56-110`；`models/operation_log.py:16-51` | 有效，但粒度=**HTTP 请求**（一次对话 1 行，覆盖 20 次工具调用） |
| **日志文件** | 无。`logging.basicConfig` 只有 StreamHandler → stdout | `main.py:15` | 取决于运维是否重定向；无工具级结构化行 |
| **日志（工具级）** | React 每次工具一行：`[react] tool_call name=... result_length=...`（**无 args、无耗时、无 session**） | `react_agent.py:360` | 有，但不可关联 |
| **日志（SQL）** | 只有**被拦截**时打 layer/rule/reason/sql（sql 截 500）；执行失败只打 `Query tool error: %s`（无 datasource/session） | `agent_tools.py:576-579`；`agent_tools.py:747` | 有，但半信号 |
| **日志（LLM 用量）** | `[llm] call model=.. in=.. out=.. cached=..` | `llm_client.py:76-95,140` | 有，但**无 session/turn 关联**，无法归因 |
| **Trace/Span（Langfuse）** | `lead_agent_turn`（带 user_id/session_id）+ 各 LLM generation + 部分 tool span | `lead_agent.py:482-488`；`lead_decider.py:356`；`tool_executor.py:237` | ❌ 默认关闭（`config.py:119`、`.env:30`）→ 降级为本地 SpanRecord |
| **Trace 元数据（本地降级）** | `trace.metadata` **不会被打印**（本地模式只打 trace 名/耗时/子 span 数） | `observability.py:210-233` | ❌ 写进去就丢：`step_stats`（attempts/retries，`agent_orchestrator.py:835-849`）、React 的 iterations/done_reason（`react_agent.py:109-115,437-443`）全部只进内存 |
| **SSE 事件** | `tool_call{name,args}` / `tool_result{name,result}`（result 全量、不截断） | `tool_executor.py:162,189` → `chat_task.py:372-394` / `canvas_task.py:95-111` / `ai.py:2036-2053` | 有，但**流式无持久**——除 Redis 事件日志外无副本 |
| **Redis 事件日志** | 上述全部事件按序 XADD，可回放 | `event_stream.py:85-104`；`chat_task.py:328-342` | ⚠️ TTL 3600s（`event_stream.py:55`）、maxlen 2000（`:44`）、每轮开始清空（`ai.py:1020`） |
| **内存结构 tool_chain** | `[{name,args<=240,ok,rows}]` | `lead_tools.py:722,726-736,788-793` | ❌ 只被 `lead_review` 消费生成摘要（`lead_review.py:100-111`），请求结束即消失 |
| **图引擎 steps_log** | 每个节点的 route + elapsed_ms | `graph.py:143-155` | ❌ 塞进 `state["__steps__"]` 后**无人读取**（全仓 grep 仅测试/注释命中） |
| **前端** | 工作台步骤 + 工具名/args/result（内存） | `aiChatStore.ts:279-337`；`canvasAssistantStore.ts:223-284` | 刷新即丢（需重放才能恢复） |

### A.2 缺口清单（每条都有代码证据）

| # | 缺口 | 证据（file:line） | 后果 |
|---|---|---|---|
| ① | **无 trace_id / request_id**：全仓无 `X-Request-ID`、无 `trace_id` 变量 | grep `request_id\|trace_id\|correlation` 在 `app/` 下无命中（唯一 `correlation` 是 `statistics.py:255` 的业务相关分析） | 日志、Redis、DB、前端四处的记录无法互相关联 |
| ② | **无 tool_call_id 传递**：LLM 的 `tc["id"]` 在 emit 时被丢弃 | `tool_executor.py:162`（`tc` 里有 id，见 `:36,202`）；`chat_task.py:373`、`canvas_task.py:96-97` | 前端只能**按工具名 + status==run** 配对（`aiChatStore.ts:325`）；同名工具并发时配错对 |
| ③ | **无耗时**：工具执行器没有计时；React 日志只有结果长度 | `tool_executor.py:231-251` 无 `time.*`；`react_agent.py:360`；只有整请求耗时（`middleware.py:71-73`）和 `run_analysis` 的 `elapsed_ms`（`lead_tools.py:958,979`，且不落库） | 无法回答「这次慢在 LLM 还是工具还是 DuckDB」 |
| ④ | **入参出参不可留存（或脱敏后留存）**：span 里 output 硬截 300 字符 | `tool_executor.py:250`；`observability.py:146-163`（默认 limit=500）；`lead_tools.py:726-736`（args 截 240） | 排障要全文时没有；且当前策略是「要么全存（Redis 里有明文 SQL）、要么丢」，没有脱敏层 |
| ⑤ | **无失败原因分类**：错误一律 `{"error": str(e)}` 自由文本 | `tool_executor.py:243`；`agent_tools.py:580,613,620,642,749,777` | 分类信息**已经存在但被丢弃**：`GuardResult.layer`（1/2/3，`sql_guard.py:78-96`、`:262-274`）、AST `failed_rule`（`agent_tools.py:575` 只在日志里打过） |
| ⑥ | **无 token 成本归因**：usage 只进日志；只有**非流式**调用把 usage 写进 span | 写入点仅 5 处：`planner_agent.py:193-196`、`agent_orchestrator.py:892-896,939-943`、`lead_decider.py:363-369`、`lead_agent.py:1007-1011`；React 流式循环无 usage（`react_agent.py:152-154`），画布执行器不取 usage（`canvas_orchestrator.py:394-396`；`complete_with_tools` 内部调 `_log_usage` 但 `llm_client.py:355` 不返回 meta） | 成本账单算不全；无法定位「哪一步烧的钱」 |
| ⑦ | **无重试次数**：唯一实现写进 trace.metadata（默认部署即丢）；`ToolExecutor` 把 attempt 硬编码为 1 | `agent_orchestrator.py:835-849` + `observability.py:210-233`（不打印 metadata）；`tool_executor.py:250` | 重试/自纠错次数不可见（这是本仓最贵的成本项之一） |
| ⑧ | **缺一层执行覆盖**：画布编排器的 `ToolExecutor` **没有传 trace** → 该路径连内存 span 都没有 | `canvas_orchestrator.py:351-362`（对比 `agent_orchestrator.py:500-514` 有 `trace=shared.get("trace")`） | `docs/wiki/21-ai-observability-eval.md:15` 声称「每次工具调用（tool span）」——**与代码不符** |
| ⑨ | **观察类工具不留任何痕迹**：`recon_tool` 直接调函数，不进 `ToolRegistry`/`ToolExecutor` | `lead_observe.py:206-265`（仅建 span，不 emit、不记录结果） | Lead 的 5 个观察工具（`lead_agent_tools.py:33-39`）在 SSE/DB/日志里都看不见 |
| ⑩ | **无可查询性**：没有 `GET /audit/tool-calls` 之类端点 | `api/v1/audit.py:1` 只查 operation_logs | 事后复盘只能翻 stdout（如果有留存） |

### A.3 最小改动方案

**设计原则（三条，都以现有代码为约束）**
1. **只在 choke point 采集**：`ToolExecutor.execute_tool_call` 是全部工具调用的必经之路（React `react_agent.py:336-358`、通用编排 `agent_orchestrator.py:500-514`、画布编排 `canvas_orchestrator.py:351-362`），在此一处即可覆盖 95% 调用。
2. **写库不在请求路径上**：复用 `chat_task._log_writer` 已验证的「缓冲 + 单写入者」范式（`chat_task.py:321-342`），不新增基础设施。
3. **脱敏必须复用已有策略**，不能新写一套：列级敏感识别已有（`sensitive_columns.py:21-45`），凭据「只进内存不落日志」已有约定（`credential_refs.py:16`），截断唯一所有者是压缩层（`compress.py:1-10`）。

#### A.3.1 表结构 DDL 草案（迁移 `0023_add_agent_calls.py`；当前 head = `0022_add_credential_ref.py`）

一张表承载 tool / llm / dispatch / turn 四类记录，**按 `(trace_id, seq)` 排序即完整时间线**（避免多表 join）：

```sql
CREATE TABLE agent_calls (
    id               BIGSERIAL PRIMARY KEY,
    trace_id         UUID        NOT NULL,              -- 一轮请求（= 一次 POST /ai/chat/stream）
    seq              INTEGER     NOT NULL,              -- 轮内单调序号（时间线排序键）
    kind             VARCHAR(12) NOT NULL,              -- tool | llm | dispatch | turn
    entry            VARCHAR(12) NOT NULL,              -- chat | canvas
    mode             VARCHAR(24),                       -- legacy|single_agent / worker.mode
    parent           VARCHAR(24),                       -- lead|dispatch|react|canvas_orch|agent_orch|recon
    session_id       UUID        NULL REFERENCES ai_sessions(id) ON DELETE SET NULL,
    message_id       UUID        NULL REFERENCES ai_messages(id) ON DELETE SET NULL,  -- 本轮 assistant 占位行
    user_id          UUID        NULL REFERENCES users(id)       ON DELETE SET NULL,

    -- tool 类
    tool_name        VARCHAR(64),
    tool_call_id     VARCHAR(64),                       -- 补上当前被丢弃的 LLM tool_call id
    args_json        JSONB,                             -- 脱敏 + 截断后
    args_hash        CHAR(8),                           -- 与 _make_memo_key 同构（tool_executor.py:80-83）
    ok               BOOLEAN,
    error_kind       VARCHAR(32),                       -- 见下方枚举
    error_layer      SMALLINT,                          -- sql_guard.layer（sql_guard.py:85）
    error_msg        TEXT,                              -- 截 500
    result_chars     INTEGER,
    result_preview   TEXT,                              -- 截 500
    duration_ms      INTEGER,
    attempt          SMALLINT DEFAULT 1,
    memo_hit         BOOLEAN DEFAULT FALSE,

    -- llm 类
    model            VARCHAR(64),
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    tokens_cached    INTEGER,

    -- turn 类（收尾汇总，一行）
    degraded         BOOLEAN,
    rounds           SMALLINT,
    tool_calls       SMALLINT,
    tool_failures    SMALLINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_agent_calls_trace   ON agent_calls (trace_id, seq);
CREATE INDEX idx_agent_calls_session ON agent_calls (session_id, created_at DESC);
CREATE INDEX idx_agent_calls_tool    ON agent_calls (tool_name, created_at DESC) WHERE ok = false;
```

`error_kind` 枚举（全部可从现有代码派生，**不新增业务逻辑**）：
`guard_l1` / `guard_l2` / `guard_l3`（`sql_guard.py:262-274`）· `ast_rule`（`agent_tools.py:575-579`）· `ownership`（`agent_tools.py:639-642`）· `sensitive_column`（`:648-658`）· `query_timeout`（`:739-745`）· `query_error`（`:746-749`）· `unknown_tool`（`tool_executor.py:175-178`）· `whitelist`（`:167-173`）· `bad_args`（`:39-44`）· `tool_exception`（`:242-243`）· `llm_timeout` / `llm_error`（`lead_decider.py:380-384`）· `upstream`（`llm_client.py:127-129`）。

**全文 JSONL 旁路（可选，`AGENT_CALL_FULL_LOG=true` 时开启）**：`backend/logs/agent-calls/YYYY-MM-DD.jsonl`，一行一个事件，字段 = 上表 + `args_full/result_full`（经脱敏），按天切分 + 7 天保留。理由：DB 存「能查询的骨架」，JSONL 存「排障要的全文」，两者职责分开，避免大字段撑爆 Postgres。

#### A.3.2 插入点（6 处，函数级）

| # | 函数（file:line） | 写什么 | 为什么是这里 |
|---|---|---|---|
| 1 | `ToolExecutor.execute_tool_call`（`tool_executor.py:152`）包一层计时 + 采集，实际执行仍在 `_execute_once`（`:231-251`） | tool 行：name/args/result/duration/ok/memo_hit | 三条编排路径的唯一交汇；`tc` 里已有 tool_call_id（`:36,202`） |
| 2 | 白名单拒绝分支（`tool_executor.py:167-173`） | tool 行 `error_kind=whitelist` | 越权尝试是最该被审计的一类事件，目前完全静默 |
| 3 | `run_analysis` 收尾（`lead_tools.py:972-1001`） | dispatch 行：goal/entry/mode/success/elapsed_ms/blocks_added + 把已有 `tool_chain`（`:788-793`）逐条落库 | 唯一已存在结构化工具链的地方，白白丢弃 |
| 4 | `lead_observe.recon_tool`（`lead_observe.py:206-265`） | tool 行：`parent=recon`、name、ok、degradation 原因 | 观察工具绕过 registry/executor，是缺口 ⑨ 的唯一修法 |
| 5 | LLM 调用 5 个写入点补 usage（`lead_decider.py:363-369`、`lead_agent.py:1007-1011`、`planner_agent.py:193-196`、`agent_orchestrator.py:892-896,939-943`）+ React/画布执行器补取 usage（`react_agent.py:152`、`canvas_orchestrator.py:394`：`complete_with_tools` 需要返回 usage） | llm 行：model/tokens/duration | 补缺口 ⑥ 的唯一位置就在 usage 产生处（`llm_client.py:140-152`） |
| 6 | 两个任务收尾 `chat_task.py:479-525` / `canvas_task.py:193-230` | turn 行 + flush + 回填 `message_id` | 此处 `assistant_msg_id` 已在作用域内（`chat_task.py:192-199`），DB session 也在（`:360`） |

**关联 session/message/turn 的具体写法**（不改调用方签名的关键）：
- `trace_id` 在入口生成：`ai.py:667` / `ai.py:1723` 各加一行 `trace_id = uuid.uuid4()`，放进 `LeadContext` 与任务闭包参数；
- `ToolExecutor` 构造函数已是 keyword-only（`tool_executor.py:102-115`），加 `trace_id`/`message_id`/`recorder` 三个默认参数即可，**调用点零改动**；
- `seq` 用执行器自身计数器维护（不要塞进 `context` dict —— 那个 dict 会被注入给工具参数，见 `tool_executor.py:131-150`，会污染签名）；
- SSE 侧给 `tool_call`/`tool_result` 补 `tool_call_id`（数据源已在 `tc` 里），向下兼容：前端按 name 配对的逻辑可保留（`aiChatStore.ts:325`），新字段用于精确配对。

#### A.3.3 脱敏与截断

| 对象 | 策略 | 依据 |
|---|---|---|
| SQL 字面量 | 用已有依赖 `sqlglot>=25.0`（`requirements.txt:45`）重新 parse → 把所有 `Literal` 替换为 `?`；parse 失败退化为正则 `'[^']*'` → `'?'` | sqlglot 已被 sql_guard 使用，无新依赖 |
| 敏感列名 | 若 SQL 引用了 `sensitive_names` 里的列 → `error_kind=sensitive_column` 并把 args_json 整体置为 `"[omitted]"` | 复用 `sensitive_columns.py:21-45`、`agent_tools.py:644-658` |
| 连接串/密码 | **永不记录**：args 只放行白名单键（`datasource_id/sql/goal/gap/text/question`…），其余键统一 `"<masked>"` | `credential_refs.py:16`（「只进内存，不落库、不写日志」）是既有硬约定 |
| 大字段 | `args_json` <= 2KB、`result_preview` <= 500 字符、`error_msg` <= 500；全文进 JSONL 且截到 64KB | 换算系数复用 `budgets.py:26-33`（JSON 0.4 token/字）保持口径一致 |
| 行数据 | result 里 `rows` 只存行数与列名（不存单元格） | 与 `compact_result_json` 策略一致（`compress.py:57-77`） |

#### A.3.4 不阻塞主链路

```python
# services/agent_trace.py（新文件，约 120 行）
_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=2000)
_DROPPED = 0

def record(ev: dict) -> None:          # 同步、零 await、绝不抛
    try:
        _queue.put_nowait(ev)          # 满则丢弃并计数，主链路永不等待
    except asyncio.QueueFull:
        _DROPPED += 1

async def _flusher() -> None:          # 单写入者，与 chat_task._log_writer 同构
    batch: list[dict] = []
    while True:
        ev = await _queue.get()
        batch.append(ev)
        if len(batch) >= 50 or _queue.empty():
            await _flush(batch)        # 一次 executemany；失败只 warning + 重试 1 次
            batch.clear()
```

- **进程内合并**：50 条或 200ms 一次 `INSERT ... executemany`，一条连接、一次事务；
- **跨进程**：若 `TASK_QUEUE_ENABLED=true`，用已有 ARQ（`worker.py:67-97`）投递 `persist_agent_calls` job 写全文 JSONL；DB 批量仍留在进程内（延迟低）；
- **失败隔离**：flush 异常只 `logger.warning`（与 `event_stream.publish` 的降级语义一致，`event_stream.py:102-104`）；
- **开关与采样**：`AGENT_CALL_LOG=off|sampled|full`；默认全量（工具调用量级=每轮 5-20 次，不值得采样），但**失败必记**（`ok=false` 无视采样率）。

### A.4 可观测性验收标准

**能看到什么**
1. 给定 `trace_id`，一条 SQL 查出本轮完整时间线（decision → dispatch → 每个 tool → LLM usage → 收尾），按 `seq` 有序；
2. 给定 `session_id`，看到每轮的 `rounds / tool_calls / tool_failures / degraded`；
3. 给定 `tool_name` + 时间窗，得到 p50/p95 耗时与失败率；
4. 给定 `error_kind`，得到「被 guard 拦了什么 / 被白名单拒了什么 / SQL 错在什么规则」。

**能回答的运维问题（可判定）**

| 问题 | 用哪几个字段回答 | 现在能否回答 |
|---|---|---|
| 这轮为什么没出图？ | 按 seq 过滤 `ok=false` + `error_kind` | ❌（只有最终文案） |
| 慢在哪？ | `duration_ms` 按 `kind` 分组（llm vs tool vs turn） | ❌ |
| 是不是 LLM 在反复重试？ | 同 `args_hash` 的 tool 行计数（= 现有 `_make_fail_key` 口径，`canvas_orchestrator.py:454`） | ❌ |
| 是不是被安全策略拦了？拦在第几层？ | `error_kind` + `error_layer` | ⚠️ 只有 stdout 的 `GUARD BLOCKED` 行（`agent_tools.py:576-579`） |
| 一轮花了多少钱？ | `tokens_in/out/cached` 按 trace_id 求和 | ❌（只有无关联的 `[llm] call` 行） |
| 哪个工具最容易失败？ | 按 `tool_name` 聚合 `ok=false` 比例 | ❌ |
| 画布入口 vs 对话入口，哪个更差？ | 按 `entry` 分组成功率/耗时 | ❌ |
| memo 命中率（省了多少重复查询） | `memo_hit` 占比 | ❌ |

**回归方式**：新增 `tests/test_agent_calls_log.py`（不跑全量）：用假 `ToolExecutor` + 假 flush 断言三件事——(1) 三条编排路径都产生 tool 行；(2) 白名单拒绝产生 `error_kind=whitelist`；(3) `record()` 在队列满时不抛异常且主链路耗时 < 1ms。

---

## B. 引入 LangChain / LangGraph 的必要性

### B.1 九个能力点逐项判断

| 能力 | 现状（file:line） | 自研完成度 | LangChain/LangGraph 的增量 | 结论 |
|---|---|---|---|---|
| **LLM 调用封装** | `llm_client.py:97`（complete）/`:154`（stream）/`:209`（stream+tools）/`:304`（complete+tools）/`:363`（统一入口按配置切流式）；usage 解析 `:137-152`；模型按任务路由（`test_observability.py:130-141` 断言 `model_for_task`） | 高（4 形态 + 流式 tool_call 分片累积 + 非流式省拼接 + `include_usage`） | 多供应商抽象、内置重试/回退 | **不需要**：单供应商 + 已封 base_url（`llm_client.py:29-34`）；重试缺口应在自研层补（约 3 行） |
| **工具 schema 与校验** | `BaseTool.schema()` 手写 dict（`agent_tools.py:305`、示例 `:529-550`）；**执行前无 schema 校验**，`parse_tool_arguments` 解析失败静默返回 `{}`（`tool_executor.py:39-44`） | 中（schema 有、校验无） | Pydantic args_schema + 校验失败自动回喂 | **真实增量**，但只需「借概念」：用 pydantic 校验入参约 40 行，不必换框架 |
| **循环与中断** | 自研图引擎 `graph.py:33-156`（node/edge/条件路由/`_MAX_STEPS=100`）；React 循环 `react_agent.py:49-57`（`MAX_ITERATIONS=6`，`:30`）；中断靠会话锁 + idle guard（`chat_task.py:145-173`） | 中高 | `interrupt()/Command(resume)` 节点级挂起（能力细节「未确认」，见文末） | 概念可借鉴，替换收益低 |
| **持久化 checkpoint** | **无图级 checkpoint**。有：Redis 事件日志（`event_stream.py:55,92`）、任务态 key（`ai.py:813` / `:2371`）、`graph.steps_log`（`graph.py:143-155`，**生成即丢**） | 低 | checkpointer 落 Postgres，可「从节点续跑」 | 我们的恢复语义是「整轮重跑」（`ai.py:723-766` 事件日志回放），不是「从节点续跑」；引入前必须先外置状态，成本远大于收益 |
| **人工确认** | `ask_user(ask_kind=confirm)`（`lead_agent_tools.py:127-131`）→ `confirm_request` SSE（`lead_agent.py:632-637`）→ 前端卡片（`canvasAssistantStore.ts:285-294`）→ 下一请求 `ui_action` 守卫恢复（`lead_agent.py:839-879`）+ Redis 挂起槽（`lead_session_state.py:82`）；有单测 `test_confirm_gate.py` | 高（端到端可用） | 任意节点挂起 | 我们只需要「确认一次」，已够 |
| **流式事件** | 自研 SSE 15+ 类型；**三份翻译器**（`chat_task.py:362-462`、`canvas_task.py:73-156`、`ai.py:2020-2094`）+ Redis 回放（`ai.py:249` 定义 `_stream_from_event_log`） | 高（含断线续收，多数框架也不白送） | `astream_events` 统一事件流 | 采用需写全量适配回我方协议（前端硬编码，`aiChatStore.ts:234-373`）→ 净增工作 |
| **Tracing** | `observability.py:166-233` 自研 Trace/Span + Langfuse 可选；langfuse 已是依赖（`requirements.txt:67`）但默认关（`.env:30`） | 中（骨架有、默认无数据、metadata 本地丢失） | 现成 callback / LangSmith | **打开已有 Langfuse 即可**，不需要 LangChain |
| **多 Agent 编排** | Lead(supervisor) → 派发表（`lead_worker.py:35-49`）→ `dispatch_analysis`（`lead_dispatch.py:36-88`）→ 3 种 worker；并发用 `asyncio.gather`（`canvas_orchestrator.py:441-443`） | 高 | 子图/并行分支/状态隔离 | 我们要的「按入口选内核」是 15 行表（`lead_worker.py:43-46`），框架帮不上 |
| **评测** | `tests/agent_evals/`（`semantic_run_eval.py` / `judge.py`），语义判分 + 三路归一（`docs/wiki/21-ai-observability-eval.md:29-48`） | 中高 | dataset/experiment 管理 | 可选择性接 Langfuse datasets，但不是换框架的理由 |

**一句话**：9 个能力点里，我们的缺口只有 2 个是框架能直接补的（args 校验、tracing 后端），其余 7 个自研已覆盖甚至超出（断线续收、双入口翻译、HITL 三态机，这些框架都不白送）。

### B.2 明确「反对引入」的理由

1. **迁移面 = 约 1.5 万行**。`agents/` + `context/` + `agent_tools`/`canvas_tools`/`llm_client`/`chat_task`/`canvas_task`/`event_stream`/`observability` 39 个文件合计 **14,841 行**（本次实测，清单见文末附录 #64）。加上 `api/v1/ai.py`（2,485 行）与 `worker.py`（236 行），主链路约 17.5k 行。
2. **概念已经复刻**：`graph.py:1` 首行自述「轻量图编排引擎（LangGraph 模式，零依赖）」，并实现了 Node / Edge / Conditional / State / steps_log 五件套（`:33-156`）。再引入真 LangGraph = 同时存在两套图引擎，新人要判断哪一套生效。
3. **与「工具四道门」不变量正面冲突**。硬边界是「写工具永不注册」（`lead_agent_tools.py:25-31` + 注释 `:9-11`），并有三份白名单（`canvas_tools.py:85-89`、`ai.py:79-82`、`canvas_orchestrator.py:47-51`）与 6 个测试守卫（`test_architecture_consistency.py:53-131`）。LangChain 的 `ToolNode`/工具注册表是独立体系，会让「注册表 ∩ 写工具 = 空集」这条断言失效——**而这条断言正是为了防止画布写工具泄漏进对话入口**（`test_architecture_consistency.py:93-97`）。
4. **与 context assembler 的三条铁律冲突**：①「一律 token 口径、禁止字符比较」（`budgets.py:1-18`）；②「所有截断只允许出现在 compress」（`compress.py:1-10`）；③决策/回答/Worker 的 messages 与 legacy **逐字节等价**由测试锁定（`compress.py:8-9` 提到 `test_context_foundation.py`）。LangChain 的 message 管道无法表达「逐字节等价 + 每块 token 台账」（`lead_agent.py:438-450` 的 ctx_ledger）。
5. **SSE 协议 + 前端硬编码**：事件 shape 的任何变化都要同步改两个 store 的 `switch`（`aiChatStore.ts:234-373`、`canvasAssistantStore.ts:180-347`；未知类型走 `default` 静默丢弃，`:371-372` / `:345-346`）。框架事件流必须先降维成我方 15 种类型，等于写一层适配器——**净增代码**。
6. **调试复杂度已经很高**：一条工具调用链上有 3 个后台 task（`lead_tools.py:937-938`、`lead_dispatch.py:176`）+ 2 个队列 + 1 个 idle 看门狗（`chat_task.py:145-173`）+ 取消/关闭顺序约束（`lead_dispatch.py:119-129` 的「子执行器 done 不能透传」）。再叠一层框架的 `astream` 生命周期，排查路径会从 3 层变 5 层。
7. **依赖体积**：langchain 会引入 `langchain-core` 及若干子包，具体体积与传递依赖「未确认」（本次未安装、未测）。已知当前依赖表刻意精简（`requirements.txt:71` 明确写「探针脚本用 httpx/requests 已覆盖，无需额外依赖」），引入大依赖与既有取向相反。
8. **教学/面试叙事反向**：`docs/wiki/21-ai-observability-eval.md:17` 把「trace → span 层级与真实编排一致」当作**面试价值**在写；如果编排交给框架，这部分叙事会退化成「我会配 LangGraph」，含金量下降。

### B.3 三种落地形态与推荐

| 形态 | 内容 | 工作量 | 收益 | 风险 |
|---|---|---|---|---|
| **(a) 不引入，只借鉴概念** | 在自研 `graph.py` 上补两个概念：`Interrupt`（节点返回特殊标记时挂起）+ `Checkpoint`（把 `state["__steps__"]` 落进 `agent_calls`） | **S**（约 80 行） | 补齐「节点级可观测/可挂起」，与三份翻译器零冲突 | 低 |
| **(b) 只把 tracing/评测旁路打开** | ①`LANGFUSE_ENABLED=true` + 补 usage（A.3.2-5）+ 脱敏（A.3.3）；②`agent_calls` 表（A.3）；③可选 OTel exporter | **S**（表 + 开关 + 6 个插入点） | **解决 90% 的真实痛点**（能查、能归因、能算钱） | 低；langfuse 已是依赖，无新增包 |
| **(c) 逐步用 LangGraph 重写编排层** | 迁移路径：①先 `react_agent.py`（531 行，无子图依赖）→ ②`canvas_orchestrator.py`（633 行）→ ③`lead_agent.py` + `lead_agent_unified.py`（1,585 行）。每步都要保留事件协议适配层 | **L** | 中长期：并行分支、checkpoint、人类中断 | 高，见下方不可逆点 |

**不可逆点（一旦做，回退成本剧增）**
1. **事件协议**：一旦前端改为消费 LangGraph 事件，两个 store 的 switch（`aiChatStore.ts:234-373`）要重写 → 回退 = 前端重写。
2. **checkpointer 落库后**：状态有了「权威副本」，而当前上下文权威来源是 assembler（`provider.py:115-140`，每步重建完整 messages）。两者双写必然不一致，届时「删掉 checkpointer」等于删掉续跑能力。
3. **ToolRegistry 被替换后**：`test_architecture_consistency.py` 的 6 个断言与 `test_orchestrator_tool_whitelist.py` 会成批失效，而它们守的是**安全边界**（写工具隔离），不能简单删除。
4. **SSE 续收**：现在是「事件日志 + 游标回放」（`event_stream.py:107-160`），换成框架 astream 就失去可回放性 → 直接退化「刷新丢事件」（`event_stream.py:1-30` 记录的旧 bug）。

**推荐：企业级走 (b) + (a)（先 b 后 a），不做 (c)。**
理由排序：(b) 的收益**立即可验证**（A.4 表格里 8 个运维问题现在 8 个答不了，做完能答 7 个）；(a) 是低成本对齐概念；(c) 的收益（并行/checkpoint）在「单用户单轮、每轮 5-20 次工具调用」的当前负载下不是瓶颈——真正的瓶颈是**看不见**。

### B.4 教学版的特殊考虑

1. **学生要什么**：就业市场要「框架用法」（简历关键词），但**能讲清原理**才是面试分水岭。两者不冲突，冲突的是**放在哪个仓库**。
2. **不要污染主仓**（三条硬理由）：
   - 依赖：主仓 `requirements.txt` 刻意精简（`:71`），加 langchain 会拖慢镜像与 CI；
   - 测试：`test_architecture_consistency.py:1-18` 的存在理由就是「不变量必须有机制守着」，引入第二套 registry 会让它变红，届时要么删守卫（安全退化）要么写豁免（守卫失效）；
   - 叙事：主仓卖点是「白盒实现」，混入框架示例会让读者分不清哪些是自研。
3. **推荐形态**：独立 `teaching/langgraph-demo/`（独立 `requirements.txt`、独立 pytest 配置、CI 不纳入主仓回归），用**同一业务场景**（取数 → 出图）做三实现对照：
   - `01_raw_sdk.py`：裸 OpenAI SDK + 手写循环（= 本仓 `react_agent.py` 的思路）
   - `02_langchain_agent.py`：LangChain 工具箱 + AgentExecutor
   - `03_langgraph.py`：LangGraph 图 + checkpointer + interrupt
   课程作业：diff 三版的「工具注册 / 循环 / 中断 / 观测」四件事，写出「框架替我做了什么、代价是什么」。这比「教框架 API」更能建立判断力，也能直接复用主仓的真实场景。
4. **若一定要进主仓**：最低成本做法是「只进文档不进代码」——在 `docs/research/` 放对照说明 + 代码片段（不装依赖、不进测试），可运行 demo 留在独立仓。

---

## C. 对话 / 画布解耦的代码级成本

### C.1 当前两个入口共享的所有东西

| # | 资产 | 位置（file:line） | 是否已按入口分化 |
|---|---|---|---|
| 1 | **决策 prompt** | `ai_prompts.py:704`（`LEAD_MERGED_SYSTEM_CANVAS` vs `LEAD_MERGED_SYSTEM`），yaml 在 `prompts/lead_merged_system*.yaml` | ✅ **已分化**，且被测试锁住（`test_architecture_consistency.py:171-198`、`test_prompt_config_consistency.py:65-109`） |
| 2 | **Worker/执行 prompt** | 共用 `AGENT_SYSTEM`（`lead_tools.py:813,827`）、`EXECUTOR_SYSTEM`（`agent_orchestrator.py:477,529`）；画布专用 `CANVAS_PLANNER_SYSTEM` / `CANVAS_EXECUTOR_SYSTEM`（`canvas_orchestrator.py:104-111`） | ⚠️ 部分：react 路径的「落块契约」靠运行时追加 system 消息（`lead_tools.py:831-864`） |
| 3 | **System prompt（单 Agent 循环）** | `LEAD_AGENT_SYSTEM` 单份（`lead_agent_unified.py:226,257`） | ❌ 未分化（`LEAD_AGENT_MODE=single_agent` 时两入口同一份） |
| 4 | **工具注册表** | 单例 `ToolRegistry`（`agent_tools.py:313-334`）；画布工具在 `canvas_tools.py:968-976` 底部注册（同一注册表，只是别名 `ToolRegistry as _CanvasToolRegistry`，`lead_tools.py:624`） | ❌ 共享（单源，这是优点） |
| 5 | **工具可见性「四道门」** | `WRITE_TOOL_NAMES`（`lead_agent_tools.py:25-31`）、`CANVAS_TOOL_NAMES`（`canvas_tools.py:85-89`）、`CANVAS_ALLOWED_TOOL_NAMES`（`ai.py:79-82`）、`_EXECUTOR_TOOL_NAMES`（`canvas_orchestrator.py:47-51`）、`AGENT_HIDDEN_TOOLS`（`agent_tools.py:53-56`） | ✅ 已分化，但**分散在 5 个模块**，靠测试对齐（`test_architecture_consistency.py:63-131`） |
| 6 | **上下文装配** | `TurnContextProvider`（`provider.py:53-140`）+ `Assembler` + 6 个 Source（`provider.py:73-80`） | ❌ 共享（唯一出口；入口差异只体现在 `canvas_layout` 是否为空，`lead_agent.py:528-541`） |
| 7 | **观察能力（Lead 的眼睛）** | `lead_observe.py:27-71` + `recon_tool`（`:206-265`）；工具表写死 5 个观察工具（`lead_agent_tools.py:33-39,89-100`） | ❌ 共享：**chat 入口下 `get_canvas_layout` 是死工具**（`canvas_state.py:69-71` 要求 `entry=="canvas"` 才 enabled → `lead_observe.py:27-38` 恒返回空串） |
| 8 | **事件协议（翻译器）** | **三份**：`chat_task.py:362-462`（对话）、`canvas_task.py:73-156`（画布·队列路径）、`ai.py:2020-2094`（画布·进程内路径，与前者逐行同构） | ❌ 共享类型名、**实现三份**（`canvas_action` 提取只在画布两份：`canvas_task.py:102-111`、`ai.py:2044-2053`） |
| 9 | **前端消费方** | 两个 store：`aiChatStore.ts:234-373` / `canvasAssistantStore.ts:180-347`；两个页面：`pages/AIChat/index.tsx:19` / `pages/FreeCanvas/components/AIAssistant.tsx:3` | ✅ 已分离（但 event reducer 是复制关系，非共享） |
| 10 | **状态存储** | `AISession.entry` / `canvas_id`（`models/ai_session.py:24-31`、索引 `:14-16`）；会话锁（`session_lock.py:26-45`）；任务态 key 前缀不同（`ai.py:813` vs `ai.py:2371`）；HITL 挂起槽共享（`lead_session_state.py:82`） | ✅ 已分化（entry 列 + key 前缀） |
| 11 | **LeadAgent 构造点** | 4 处：`ai.py:1004`（对话·进程内）、`ai.py:2013`（画布·进程内）、`worker.py:144`（对话·队列）、`worker.py:207`（画布·队列） | ❌ 完全重复（参数只有 `extra_plannable_tools` 不同） |
| 12 | **交付契约** | 画布「必须落块」（`lead_tools.py:831-864`、`canvas_orchestrator.py:53-56` 的 `_MAX_NO_TOOL_STREAK`）；对话「必须出报告」（`react_agent.py:498-530` 的 follow_up 分支） | ✅ 语义已分化，但都写在 worker 内部而非「画像」里 |

**结论**：共享点里 6 项已经分化（1、5、9、10、12 及 2 的一部分），真正「耦合且有害」的是 **8（三份翻译器）、11（四处构造）、7（死工具）、3（单 Agent 提示词未分化）**。

### C.2 按「能力画像」（EntryProfile）解耦

**目标数据结构**（新文件 `backend/app/services/entries/profile.py`，S）：

```python
@dataclass(frozen=True)
class EntryProfile:
    entry: str                        # chat | canvas
    decision_prompt: str              # LEAD_MERGED_SYSTEM / _CANVAS
    agent_system: str                 # LEAD_AGENT_SYSTEM / 新增的 _CANVAS 版
    allowed_tools: frozenset[str]     # 入口白名单（替代 CANVAS_ALLOWED_TOOL_NAMES 常量）
    observe_tools: frozenset[str]     # 观察工具子集（chat 不含 get_canvas_layout）
    worker_table: dict[str, str]      # 复用 lead_worker.ENTRY_WORKERS
    emit_canvas_action: bool          # 画布 True → 翻译器提取 canvas_action
    finalize: Callable                # 兜底文案策略（canvas_task.py:161-166 vs chat 无）
```

| 改动项 | 文件 / 函数 | 量级 | 风险点 |
|---|---|---|---|
| 新增 `EntryProfile` + 注册表 | 新文件 `services/entries/profile.py`、`registry.py` | **S** | 与 `ENTRY_WORKERS`（`lead_worker.py:43-46`）重复定义 → 必须单向引用（profile 引用 worker 表，不要复制） |
| 白名单常量迁入画像 | `canvas_tools.py:85-89`、`ai.py:79-82`、`canvas_orchestrator.py:47-51`、`agent_tools.py:53-56` | **M** | `test_architecture_consistency.py:63-131` 直接 import 这些常量（`:27,36`）→ 必须同步改测试；漏改则 6 个守卫失效 |
| 三份翻译器合并 / 去重 | `chat_task.py:362-462`、`canvas_task.py:73-156`、删除 `ai.py:1970-2164`（改调 `canvas_task.run_canvas_task`） | **M→L** | ①两入口事件差异是**真实的**（`canvas_task.py:10-15` 文件头明确说「别共用一个函数」）；②删内联任务体要保证「进程内 + 队列」双路径等价，`test_canvas_resume_contract.py:80-97` 会检查事件日志顺序（先 publish 后 registry） |
| 观察工具按入口裁剪 | `lead_agent_tools.py:33-39,89-100,136-142` + `lead_agent_unified.py:158`（`tools = lead_tool_specs()`） | **S** | 消除「chat 里 `get_canvas_layout` 死工具」；注意 `decision_action` / `tool_class`（`:145-170`）依赖全集判定，裁剪后要为未知工具留降级（`:251-254` 已有） |
| 单 Agent 循环提示词分化 | `ai_prompts.py` 新增 `LEAD_AGENT_SYSTEM_CANVAS`；`lead_agent_unified.py:226,257` 改取画像 | **S** | `test_prompt_config_consistency.py:88-109` 只锁定 `lead_merged_system`，新 yaml 需补一条同样的「yaml == 常量」守卫，否则又会静默回落 fallback |
| LeadAgent 构造收敛 | `ai.py:1004`、`ai.py:2013`、`worker.py:144`、`worker.py:207` → 新 `services/entries/factory.py: build_lead(profile, payload)` | **M** | `worker.py:36-41` 已因「不能 import api 层」而复制过一份白名单；工厂放 `services/` 才能被两进程共用（`worker.py:100-155` 的 payload 形状也要对齐） |
| 前端 event reducer 抽公共 | `aiChatStore.ts:234-373`、`canvasAssistantStore.ts:180-347` → `frontend/src/lib/sseReducer.ts` + 两入口各自的「副作用表」 | **M** | `progress` 过滤规则两处注释相同但实现不同（`aiChatStore.ts:242-264` vs `canvasAssistantStore.ts:188-212`，后者多 `seq`）；合并时要保留差异，别「统一」成退化 |

**总评：C.2 = S/M 为主 + 1 项 M-L，约 1-2 周（含回归）。**

### C.3 按「两个独立 Lead 循环」解耦

**新增文件（估算约 2,800-3,500 行）**
- `services/entries/lead_chat/agent.py`（从 `lead_agent.py` 拆：决策 + 回答 + 记忆回流，约 600 行）
- `services/entries/lead_chat/decider.py`（分离对话版决策 prompt / 解析）
- `services/entries/lead_canvas/agent.py`（决策 + 确认守卫 + 落块验收，约 700 行）
- `services/entries/lead_canvas/review.py`（画布专用 `assess_subtask_detail`：`min_blocks` / `verified` 口径，`lead_tools.py:101-213`）
- `services/entries/{profile.py, registry.py, factory.py}`
- 前端：`pages/AIChat/eventReducer.ts`、`pages/FreeCanvas/eventReducer.ts`
- 测试：`tests/test_entry_profiles.py`、`tests/test_lead_chat_loop.py`、`tests/test_lead_canvas_loop.py`

**必须继续共享（不能各写一份）**：`llm_client`（含 usage 解析）、`ToolRegistry` + 全部工具实现、`context/` 四件套（`provider.py:53-140`）、`event_stream`、`session_lock`、`observability`、`chat_task` / `canvas_task` 的收尾与落库逻辑、`ai_messages` / `ai_sessions` 表。

**回归测试必须覆盖（`backend/tests/` 文件名）**
- 架构/一致性：`test_architecture_consistency.py`、`test_prompt_config_consistency.py`
- Lead 主管层：`test_lead_merged.py`、`test_lead_phase4_fallback.py`、`test_lead_phase5.py`、`test_lead_phase6_monitoring.py`、`test_lead_p1_unified_loop.py`、`test_lead_p1_agent_tools.py`、`test_lead_p1_intent_signal.py`、`test_lead_p1_ask_kind_guard.py`
- 评审/派发：`test_lead_p0_capabilities.py`、`test_lead_p2_review_stage.py`、`test_lead_p25_repair_memo.py`、`test_lead_p3_observe_acceptance.py`、`test_lead_p3_query_sample.py`
- 上下文/压缩：`test_lead_p4_assembly_unified.py`、`test_lead_p4_tool_result_compression.py`、`test_context_assembly.py`、`test_context_foundation.py`、`test_window_ownership.py`
- HITL：`test_confirm_gate.py`、`test_lead_canvas_state.py`
- 流式/会话：`test_ai_session_entry.py`、`test_chat_task_error_paths.py`、`test_canvas_task_queue.py`、`test_canvas_resume_contract.py`、`test_event_stream_queue.py`、`test_chat_stream_registry.py`、`test_sessions_running_flag.py`、`test_task_state_write_guard.py`、`test_session_lock.py`
- 工具/内核：`test_tool_executor_memo.py`、`test_tool_call_streaming_scope.py`、`test_canvas_orchestrator.py`、`test_canvas_orchestrator_tools.py`、`test_canvas_tools.py`、`test_canvas_action_ledger.py`、`test_canvas_block_writer.py`、`test_react_agent_limits.py`、`test_orchestrator_tool_whitelist.py`、`test_orchestrator_advanced.py`、`test_orchestrator_fallback_timeout.py`、`test_agent_flows.py`、`test_reasoning_budget.py`、`test_planner_validation.py`、`test_lead_memory_merge.py`

**前端**：`stores/__tests__/canvasAutoResume.test.ts`、`stores/__tests__/confirmCard.test.ts`、`stores/__tests__/canvasAssistantStore.test.ts`

**总评：C.3 = L（20+ 后端测试文件 + 3 前端测试），且收益可疑**——两个入口的「决策大脑」差异主要是 prompt 与工具集，而这两件在 C.2 里已经能被画像表达；拆成两套循环会**复制**「确认守卫 / 记忆回流 / 收尾 done / 评审闸」这四段高价值逻辑（`lead_agent.py:629-641,762-769,891-907`），而复制的正是本仓反复踩坑的地方（`lead_dispatch.py:56-59` 自述「两套实现迟早跑偏」）。

### C.4 推荐路径

1. **先做 C.2 的 S 项**（画像结构 + 观察工具裁剪 + 提示词分化）——成本最低，直接消除「chat 里死工具」和「单 Agent 提示词不分入口」两个真问题；
2. **再做 C.2 的 M 项**（白名单迁入画像 + 构造收敛为工厂 + 删掉 `ai.py` 内联画布任务体）。**三份翻译器先「合并两份」**（删 `ai.py:1970-2164`，与 `canvas_task.py` 合流），保留「对话 / 画布两个 translator + 一个 profile 开关」的结构——因为两入口的事件差异是真实存在的（`canvas_task.py:10-15`）；
3. **不做 C.3**（两个独立 Lead 循环）。若未来真要拆，前置条件先满足：`agent_calls` 表（A.3）上线，能用数据证明「两入口的决策路径确实不同到必须分家」——目前没有这个证据。

---

## 附：证据索引表

| # | file:line | 事实 |
|---|---|---|
| 1 | `config.py:119` / `.env:30` | Langfuse 默认关闭 |
| 2 | `config.py:233` | `LEAD_AGENT_MODE` 默认 legacy |
| 3 | `config.py:240` | `TASK_QUEUE_ENABLED` 默认 false（进程内执行） |
| 4 | `main.py:15` | 日志仅 stdout，无文件 handler |
| 5 | `event_stream.py:44,55` | 事件日志 maxlen 2000 / TTL 1h |
| 6 | `ai.py:1020` / `ai.py:2387` | 每轮开始清空事件日志 |
| 7 | `chat_task.py:493-497` | 只落 assistant 正文 + charts |
| 8 | `middleware.py:56-110` | operation_logs 只到 HTTP 粒度 |
| 9 | `tool_executor.py:152-203` | 工具执行唯一 choke point；emit 无 tool_call_id、无耗时 |
| 10 | `tool_executor.py:231-251` | span output 截 300、attempt 硬编码 1、异常转自由文本 |
| 11 | `tool_executor.py:250` | `metadata={"ok":..., "attempt": 1}` |
| 12 | `observability.py:210-233` | 本地降级模式不打印 trace.metadata |
| 13 | `agent_orchestrator.py:835-849` | 唯一的 retries 统计，写进会被丢弃的 metadata |
| 14 | `canvas_orchestrator.py:351-362` | 画布执行器 `ToolExecutor` 未传 trace |
| 15 | `agent_orchestrator.py:500-514` | 通用编排器传了 `trace=shared.get("trace")` |
| 16 | `react_agent.py:360` | React 工具日志只有 name + result_length |
| 17 | `lead_observe.py:206-265` | 观察工具绕过 registry / executor |
| 18 | `lead_tools.py:722,788-793,981` | 内存 tool_chain（args 截 240） |
| 19 | `lead_review.py:100-111` | tool_chain 唯一消费者 |
| 20 | `graph.py:143-155` | `__steps__` 生成后无人读 |
| 21 | `sql_guard.py:78-96,262-274` | `GuardResult.layer` 已有分类，但未进工具结果 |
| 22 | `agent_tools.py:576-579` | guard 拦截时把完整 SQL 打进日志（截 500） |
| 23 | `agent_tools.py:580,642,749` | 失败只返回自由文本 error |
| 24 | `llm_client.py:76-95,140` | tokens 只进日志，无 session 关联 |
| 25 | `lead_decider.py:363-369` 等 5 处 | 只有非流式调用把 usage 写进 span |
| 26 | `canvas_orchestrator.py:394-396` | 画布执行器非流式但未取 usage |
| 27 | `chat_task.py:325-342` | 已有的「缓冲 + 单写入者」范式（可复用） |
| 28 | `worker.py:67-97` | ARQ 入队范式（可复用做全文旁路） |
| 29 | `sensitive_columns.py:21-45` | 敏感列识别可复用于脱敏 |
| 30 | `credential_refs.py:16` | 「凭据只进内存不落日志」既有约定 |
| 31 | `compress.py:1-10` | 截断唯一所有者铁律 |
| 32 | `budgets.py:26-33` | token/字符换算系数（JSON 0.4） |
| 33 | `requirements.txt:45,67` | sqlglot / langfuse 已是依赖；`:71` 明确「无需额外依赖」取向 |
| 34 | `llm_client.py:97,154,209,304,363` | 5 个 LLM 调用入口 |
| 35 | `graph.py:1-18,33-156` | 自研 LangGraph 风格图引擎 |
| 36 | `test_architecture_consistency.py:53-131` | 工具四道门 + worker 表 + prompt 分化的守卫 |
| 37 | `test_prompt_config_consistency.py:28-109` | getattr 默认值漂移守卫 + 提示词双份守卫 |
| 38 | `lead_agent_tools.py:25-31` | WRITE_TOOL_NAMES 硬边界（写工具永不注册） |
| 39 | `canvas_tools.py:85-89,968-976` | 画布工具名 + 注册到同一 ToolRegistry |
| 40 | `ai.py:79-82` | CANVAS_ALLOWED_TOOL_NAMES |
| 41 | `canvas_orchestrator.py:47-51` | _EXECUTOR_TOOL_NAMES |
| 42 | `agent_tools.py:53-56,313-334` | AGENT_HIDDEN_TOOLS + ToolRegistry |
| 43 | `provider.py:53-140` | 上下文装配唯一出口 |
| 44 | `lead_agent.py:438-450` | ctx_ledger 上下文台账 |
| 45 | `lead_agent.py:482-488` | trace 名 `lead_agent_turn` + metadata |
| 46 | `lead_agent.py:632-637,839-879` | HITL 确认链路（后端） |
| 47 | `canvasAssistantStore.ts:285-294` | HITL 确认卡片（前端） |
| 48 | `lead_session_state.py:82` | 挂起目标 Redis 槽 |
| 49 | `chat_task.py:362-462` | 对话事件翻译器 |
| 50 | `canvas_task.py:73-156` | 画布事件翻译器（队列路径） |
| 51 | `ai.py:2020-2094` | 画布事件翻译器（进程内路径，与 50 同构） |
| 52 | `aiChatStore.ts:234-373` / `canvasAssistantStore.ts:180-347` | 前端两套 event switch（未知类型静默丢） |
| 53 | `models/ai_session.py:14-16,24-31` | entry / canvas_id 入口隔离 |
| 54 | `ai.py:1004,2013` / `worker.py:144,207` | LeadAgent 4 处重复构造 |
| 55 | `canvas_state.py:69-71` | enabled 要求 entry=="canvas" → chat 下 get_canvas_layout 为死工具 |
| 56 | `lead_agent_tools.py:33-39,89-100,136-142` | Lead 观察工具表（含 get_canvas_layout） |
| 57 | `lead_worker.py:35-49` | 派发表（entry × complexity → worker） |
| 58 | `lead_tools.py:831-864` | 画布落块契约（运行时追加 system 消息） |
| 59 | `lead_dispatch.py:56-59,119-129` | 「两套实现迟早跑偏」的自述 + done 不透传约束 |
| 60 | `canvas_task.py:10-15` | 「别共用一个函数」的显式设计决策 |
| 61 | `docs/wiki/21-ai-observability-eval.md:15` | 文档声称「每次工具调用有 tool span」——与 #14 不符 |
| 62 | `docs/wiki/21-ai-observability-eval.md:17` | 可观测性被当作面试价值点 |
| 63 | 迁移编号 | 当前 alembic head = `backend/alembic/versions/0022_add_credential_ref.py`（新表迁移 0023） |
| 64 | 本次实测 | 编排/上下文/工具/流式核心 39 个文件合计 **14,841 行**：graph 142、base_agent 86、planner_agent 352、react_agent 531、agent_orchestrator 1156、canvas_orchestrator 633、tool_executor 214、lead_agent 1037、lead_agent_unified 548、lead_decider 522、lead_dispatch 255、lead_observe 278、lead_tools 1005、lead_worker 69、lead_review 123、lead_review_stage 149、lead_perception 155、lead_intent 226、lead_intent_signal 98、canvas_state 118、lead_agent_tools 170、assembler 350、provider 189、compress 210、budgets 200、types 170、window 69、memory 21、state 149、agent_tools 1927、canvas_tools 975、llm_client 402、chat_task 475、canvas_task 230、event_stream 176、chat_stream_registry 144、observability 350、prompt_registry 138、ai_prompts 799 |

### 「未确认」清单

1. **LangChain / LangGraph 的具体行为与体积**：文中关于 `interrupt()/Command(resume)`、checkpointer 后端、langchain 依赖体积的表述依据公开文档的一般认知，**未在本仓验证**（未安装、未运行）。
2. **生产环境日志是否被外部采集**：本仓只有 stdout（`main.py:15`）；是否有容器日志/ELK 侧采集在 `docker-compose.prod.yml` / `nginx.conf` 之外配置，未逐一核对。因此 A.1 的「日志文件 = 无」限定为「应用层无文件落盘」。
3. **ARQ 重试的幂等性**：`worker.py:12-13` 声称「白送重试」，但工具调用的副作用（落块依赖前端写库，`canvas_tools.py:1-6`）是否可安全重放，未确认。
4. **前端是否存在第三处 SSE 消费**：`hooks/useSSE.ts:11` 指向 `POST /ai/sessions/{id}/messages`（非流式接口），是否为遗留死代码未确认（grep 未见页面引用）。
5. **`agent_evals` 的实际通过率**：wiki 记录 90.8%（`docs/wiki/21-ai-observability-eval.md:45`），本次未复跑，仅引用不作为审计结论。
