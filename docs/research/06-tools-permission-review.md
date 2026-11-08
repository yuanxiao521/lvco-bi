# 06 · 产品级架构复审 B：工具调用（tool calling）与 权限（authz）

> 评审立场：不以"能不能跑通"为标准，而以"这套东西能不能作为商品卖给企业"为标准。
> 默认假设它太轻，用代码证据证明或证伪。
>
> 评审范围：`backend/app/services/{agent_tools,canvas_tools}.py`、`services/agents/**`、`api/v1/*`、`api/deps.py`、`core/{security,middleware,operation_log}.py`、`models/user.py`、前端 `ProtectedRoute.tsx / App.tsx`。
> 所有断言带 `file:line`；静态分析无法定论的标「未确认」。本文不改代码、不动 git。

---

## §0 一页判决

### 工具调用：**及格偏下——契约是"内部调用"的契约，不是"商品 API"的契约**

执行内核（`ToolExecutor`）把"解析参数→查注册→执行→观测→判定→emit"收口得很干净，超时/结果截断/幂等/白名单都有，**这不是草台班子**。但它缺三件商品级能力：① **参数契约没有校验层**（LLM 给错参数直接 TypeError，见 `tool_executor.py:239`）；② **错误没有码**（全是中文自由文本，模型只能靠猜，见 `agent_tools.py:580,613`）；③ **没有取消**（全仓无 stop/cancel 端点，见 §1.1）。其中 ③ 和"同一 Step 内 `asyncio.gather` 并发跑所有工具、共用同一个 `AsyncSession`"（`agent_orchestrator.py:692`）是会在客户现场反复出现的两个雷。

### 权限：**不及格——不是"缺行级权限"，而是"权限模型只有一层（属主），且这一层之外到处是横向读"**

认证只有 JWT + 存在性检查（`deps.py:57-81`，不校验账号状态/无租户/无 token 版本）；授权模型只有 **资源属主过滤**（`user_id`），`role` 只在 3 处被用到（`permissions.py:124`、`trash.py:148`、默认值）——即**没有 RBAC，只有"你自己的东西你能看"**；**没有行级/列级数据权限**，没有共享（只有"公开链接"），公开链接**无有效期、无密码、无撤销、软删除后仍可访问**。最要命的是两个"任何登录用户都能看全站"的接口（审计日志、用户列表），配上开放注册，构成一条**从"能注册"到"能看别人全站行为"的完整链路**。

### 三条能当场演示给客户的越权路径（不需要改代码，curl 即可）

| # | 路径 | 实际结果 | 证据 |
|---|---|---|---|
| **D1** | 用**任意一个 viewer 账号**的 token：`GET /api/v1/audit/logs?page_size=100` | 返回**全站所有用户**的操作日志：谁在什么时候、从哪个 IP、用哪个 UA、打了哪个 `resource_id`；`user_id` 参数是**可选**的，不填就是全量 | `audit.py:47-65`（`:62` 注释原文"任何登录用户可访问"）、`:181`（导出同权限）|
| **D2** | 同一个 viewer 账号：`GET /api/v1/permissions/users?page_size=100` | 返回**全体用户**的 email、display_name、角色、以及每人的数据源/画布/仪表盘数量 | `permissions.py:43-113`（`:52` 注释原文"任何登录用户可看，admin/editor/viewer 都行"）|
| **D3** | 点一次"分享"→ 拿到 `/share/{token}` → **等 30 天**（或把报表软删除）→ 再打开 | 链接**照旧能打开**。`expiresAt` 是接口现算的假字段，DB 里没有有效期列；`public.py` 也不过滤 `deleted_at`；全仓**没有撤销分享**的接口 | `dashboards.py:305`、`reports.py:167`（都是 `now() + 30 days` 现编）、`models/dashboard.py:25`（模型只有 `share_token`，无 `expires_at`）、`public.py:20,36`（无 `is_public` / `deleted_at` 条件）|

> D3 是"演示效果最好、也最伤信任"的一条：客户在评审会上当场问"这个链接能关掉吗"，答案是"删掉整个仪表盘才可以"。

---

## §1 轴 1：工具调用

### 1.1 现状表

| 维度 | 现状 | 证据 | 判定 |
|---|---|---|---|
| **注册表** | `ToolRegistry` 是**类级全局 dict**（进程单例），工具实例在 import 时一次性注册 | `agent_tools.py:313-334`（`_tools: dict[str, BaseTool] = {}`）、注册点 `agent_tools.py:1912-1924`、`canvas_tools.py:970-976` | 可接受（工具无状态） |
| **注册靠 import 副作用** | 画布 7 个工具**不是在** `agent_tools` 里注册，而是在 `canvas_tools` 底部注册，且**必须**先有人 import `canvas_tools`（注释写明由 `ai_service` 导入保证启动即注册）。若某入口没走 `ai_service`，`ToolRegistry.get("add_chart_block")` 会是 `None` → 被判"未知工具" → `fatal` 终止整步 | `agent_tools.py:1925-1926`、`canvas_tools.py:965-976` | **全局单例的真实代价** |
| **工具总数** | 注册 20 个 = 13 核心 + 7 画布；其中 6 个被 `AGENT_HIDDEN_TOOLS` 从 LLM 面隐藏（insight / clean_suggest / polish_text / recommend_charts / validate_chart / data_quality） | `agent_tools.py:53-56` | 可接受 |
| **各入口可见数** | Lead 10 个；react 路径 7 个（20−7 画布−6 隐藏）；画布执行器 7 个（含 `list_fields`） | `lead_agent_tools.py:136-142`、`lead_tools.py:415-437`、`canvas_orchestrator.py:47-51` | 可接受 |
| **写工具边界** | Lead **物理上不注册**写工具（不是"注册后拦截"），并有测试锁死 `注册表 ∩ WRITE_TOOL_NAMES = ∅` | `lead_agent_tools.py:25-31`、`tests/test_lead_p1_agent_tools.py:35-37,41-52` | **优秀**，全仓最硬的一条边界 |
| **schema 校验** | **没有**。`json.loads` 失败就退化成 `{}`，然后 `**targs` 直塞 `execute()`；无 pydantic 模型、不校验 `required`、不拒绝多余字段 | `tool_executor.py:39-44,239` | **不合格** |
| **非法参数后果** | 缺必填 → `TypeError` → 被 `except Exception` 吞成 `{"error": "missing 1 required positional argument ..."}`；各工具只能**手写**缺参守卫来兜（不一致） | `tool_executor.py:242-243`、手写示例 `canvas_tools.py:449-463` | **不合格** |
| **错误契约** | 工具层统一 `{"error": "<中文句子>"}`，**无 code / 无 retryable / 无字段级位置**；而 HTTP 层有 `{code,message}`、查询引擎有 `code="SENSITIVE_COLUMN"` | `agent_tools.py:580,613,651-657`、`tool_executor.py:176-178,243` vs `permissions.py:125-128`、`query_engine.py:749-752,766` | **不合格**（三层不一致）|
| **未知工具** | 返回 `fatal=True`，整步直接终止 | `tool_executor.py:175-178`、消费点 `agent_orchestrator.py:726-729` | 设计可接受，**但有自伤**（见 G4）|
| **幂等 / memo** | key = `tool_name:sha1(args)[:8]`（32 bit），过程内 dict，跨请求不共享；错误默认也缓存（幂等集），`query_sql` 仅成功缓存 | `tool_executor.py:80-83,205-229`、`agent_orchestrator.py:94-107` | 半合格 |
| **幂等集的真实性** | `_IDEMPOTENT_TOOLS` 里 6 个名字有 **4 个在注册表里不存在**（`list_tables` / `get_datasource_info` / `describe_table` / `analyze_column`）→ 幂等保护实际只覆盖 `list_datasources` / `list_fields` | `agent_orchestrator.py:94-101` vs `agent_tools.py:1912-1924` | **Bug（静默失效）** |
| **memo 与上下文注入的顺序** | memo key 在 `_inject_context` **之前**算（`:213` vs `:233`），注入的 `canvas_id` 不进 key → 将来若把上下文注入型工具加入幂等集，**同参数跨画布会返回上一张画布的结果** | `tool_executor.py:205-233,131-150` | **潜在（当前未触发）** |
| **并发** | 一个 Step 内**所有** tool_call 用 `asyncio.gather` 并发执行；上限 3（orchestrator）/ 4（canvas）/ 3（react） | `agent_orchestrator.py:692-694,449`、`canvas_orchestrator.py:441-443,39`、`react_agent.py:32` | **有上限但无隔离** |
| **并发与 DB 会话** | `ToolExecutor` 持一个 `db_session`，每个并发工具都拿**同一个** session | `tool_executor.py:116-117,239` | **高风险（未实测）** |
| **并发与写工具** | 画布执行器并发跑写工具；写工具是"读 blocks → 改 → 整体写回"的 read-modify-write | `canvas_orchestrator.py:441-443`、`canvas_block_writer.py:67-98`（`:77` 读、`:94` 写回） | **会丢块** |
| **超时** | 只有 SQL 执行有超时（`wait_for` + `to_thread`，默认 30s）；Step 级 30~45s；**`ToolExecutor` 自身对单个工具没有超时** | `agent_tools.py:664-667`、`config.py:55`、`agent_orchestrator.py:456`、`canvas_orchestrator.py:40,288-298` | 半合格 |
| **超时的真实性** | `to_thread` **不可取消**：超时只取消 `await`，DuckDB 线程仍在跑，连接/内存继续被占 | `agent_tools.py:664-667` | **已知缺陷（代码自证）** |
| **取消** | **没有任何"用户可调用的取消端点"**（在 `backend/app/api` 下 grep `abort|cancel_run|/stop|kill` 命中 0；只有 SSE 断路判断用的 `idle_stop`，`ai.py:288-295`，它不是取消）；SSE 断开后后台任务**故意继续跑** | `api/v1/ai.py`（全文件无 cancel 路由）、`ai.py:721-722` 注释"任务结束在后端继续跑" | **不合格（商品级硬缺）** |
| **结果大小** | `query_sql` 硬截 50 行；画布块 50 行；编排/react 路径统一走 `compact_result_json`（默认 800 token）；**画布执行器却把原始 `pr.result` 直塞消息，绕过压缩层** | `agent_tools.py:703`、`canvas_tools.py:568,31`、`compress.py:57-77`、`config.py:256`、**`canvas_orchestrator.py:447-452`** | 基本合格，**一处漏** |
| **"当前用户"从哪来** | 显式参数：`ToolExecutor(user_id=...)` → 每次 `tool.execute(user_id=..., db_session=...)`；工具内部**各自**再用 `user_id` 过滤 | `tool_executor.py:116,239`、`agent_tools.py:605-613`、`canvas_tools.py:229`、`canvas_block_writer.py:73` | **不会串（今天）** |
| **上下文注入的信任边界** | `_inject_context` 注释明写"LLM 显式传入的同名参数**优先**"→ 模型可覆盖系统绑定的 `canvas_id` | `tool_executor.py:131-150`（`:136-137`） | **今天安全，靠下游兜** |

### 1.2 对标：哪些是"通用 Agent 能力"，哪些其实是**商品的及格线**

| 能力 | 通用 Agent（Claude Code / Manus / OpenAI Agents SDK / LangGraph supervisor）怎么做 | 是不是及格线 | 我们 |
|---|---|---|---|
| **参数校验** | OpenAI Agents SDK 用 **pydantic 模型**反向生成 function schema，回调前就 `model_validate`，非法参数在**进函数前**变成结构化错误 | **是**（客户说一句"做个表"，模型编 `limit: "top10"` 就崩，是日常） | ✗ 无 |
| **写操作二次确认** | Claude Code 的 permission prompt：每次写/执行类工具调用先问用户（allow once / always / deny），白名单可配 | **是**（企业采购必问"AI 能不能不问我直接删/改数"） | ✗ 无（Lead 靠"不注册写工具"绕过，但画布执行器**会直接写**） |
| **工具沙箱** | Manus 每任务一个 VM + 文件/网络隔离；Claude Code OS 级沙箱 + 路径白名单 | **部分是**（我们不执行任意代码，但**连客户生产库**，等价风险在"连接"而非"代码"） | 部分（SQL 4 层 + 连接守卫） |
| **取消 / 中断** | 三者都有 stop，且 stop 必须**取消 in-flight 工具并回滚副作用** | **是**（客户点停止，看到还在跑 30s、还在烧 token，会认为"这软件失控"） | ✗ 无 |
| **工具级超时** | Agents SDK 每个 tool 可配 timeout，超时抛 `ToolTimeoutError` 给模型（可自修复） | **是** | 部分（仅 SQL） |
| **错误可自修复** | 错误体带 `code` + `hint`，模型按码分支重试 | **是** | 半（有中文 hint，无码） |
| **结果裁剪** | 工具结果进上下文前统一裁剪；Claude Code 截断并提示"已截断，用 offset 继续" | **是** | 基本是（一处漏） |
| **可发现性** | 工具 >15 个开始分组/子 agent 委派；description 精排 + 权限分类 | 是（规模化前提） | 半（隐藏集+入口白名单，是好苗子） |
| **幂等** | 读工具缓存、写工具要求幂等键 | 是（对我们尤其：同 SQL 重查是主要成本） | 半 |

**结论**：① **取消**、② **参数校验**、③ **错误码**、④ **写操作确认** 这四条是"客户现场一定踩、踩了会质疑产品成熟度"的及格线；⑤ 沙箱不需要通用版，但需要"连接侧"的等价物（已有雏形）。

### 1.3 差距（按严重度）

**G1（致命）没有取消 → 没有"止损"能力。**
- 证据：`api/v1/ai.py` 全文无 cancel 路由；`:721-722` 明确"任务结束在后端继续跑"。
- 规模：一个 5 步分析、每步最多 3 次工具、每次 SQL 最长 30s → 最坏 7 分钟不可中断；用户关页面后后台仍烧 LLM token。
- 商品后果：按 token/按次计费的场景，这是**直接的钱**；演示时"我点停止它还在转"是当场掉分项。

**G2（严重）参数无校验 + 错误无码 = 模型自修复率不可控。**
- 证据：`tool_executor.py:39-44,239,242-243`；错误体无 `code`（`agent_tools.py:580`）。
- 后果：同一个错误，模型要"读中文句子猜"再重试，白烧 1~3 轮 token；`"'NoneType' object is not subscriptable"` 这类文案对模型是纯噪音。

**G3（严重）并发共用一个 `AsyncSession` + 写工具 read-modify-write。**
- 证据：`tool_executor.py:117,239`（session 共享）、`canvas_orchestrator.py:441-443`（并发）、`canvas_block_writer.py:77,94`（读-改-写）。
- 两个独立故障：**(a)** SQLAlchemy `AsyncSession` 非并发安全，两协程同时 `await session.execute` 会撞 `InvalidRequestError`（**未确认**，需起 PG 实测；代码路径确定）；**(b)** 同 Step 两个 `add_chart_block` 并发 → 后写覆盖前写，**用户看到"AI 说加了两个图，画布只有一个"**。

**G4（中）幂等白名单名不副实 + 提示词指向不存在的工具。**
- 证据：`agent_orchestrator.py:94-101`（4 个幽灵工具）、`:662`（教模型调 `describe_table`）。
- 后果：`:662` 是最恶劣的一类 bug——**系统自己在诱导模型调一个会 fatal 终止整步的工具**。

**G5（中）画布路径绕过压缩层。**
- 证据：`canvas_orchestrator.py:451`（`"content": pr.result` 原样）vs `agent_orchestrator.py:716`（`compact_result_json`）。
- 量级：单块 = 50 行 × N 列 + `queryConfig`，10 列时约 20~80KB，连续 4 个写工具 = 数百 KB 直塞上下文。**不会到 4MB，但足以让上游 400 / 挤掉历史**。

**G6（中）超时不可真取消。**
- 证据：`agent_tools.py:664-667`（`wait_for(to_thread(...))`）。
- 后果：超时后 DuckDB 仍在跑那条 SQL，连接池/内存不释放；坏 SQL 反复重试 → **进程级内存压力**，不是单请求问题，是**多租户互相影响**。

### 1.4 改法（S / M / L）

| 项 | 改法 | 规模 | 落点 |
|---|---|---|---|
| **S1** | 给 `ToolExecutor.execute_tool_call` 加统一 `asyncio.wait_for(tool.execute(...), TOOL_TIMEOUT)`，超时返回 `{"error":..., "code":"TOOL_TIMEOUT"}` | S（`30 行 + 1 配置） | `tool_executor.py:231-251` |
| **S2** | 错误体标准化 `{"error","code","retryable","hint"}`；先给 4 个高频码（`UNKNOWN_TOOL` / `INVALID_ARGS` / `TIMEOUT` / `NOT_FOUND`） | S | `tool_executor.py:167-178,242-243` |
| **S3** | 删掉 `:662` 的 `describe_table`、清理 `_IDEMPOTENT_TOOLS` 幽灵项 | S（10 分钟） | `agent_orchestrator.py:94-101,662` |
| **S4** | 画布路径改用 `compact_result_json(pr.result)` | S | `canvas_orchestrator.py:451` |
| **M1** | **参数模型化**：每工具声明 `args_model: type[BaseModel]`，`ToolExecutor` 统一 `model_validate` → `INVALID_ARGS` 带字段路径；schema 由模型反向生成（20 个工具，2~3 天） | M | `agent_tools.py:285-311` |
| **M2** | **取消**：`POST /ai/sessions/{id}/cancel` → 置 Redis 取消位 → 编排循环每步开头检查 → 抛 `CancelledError` 走现有 finally 收尾 | M（1~2 天） | `ai.py` 新增路由；`agent_orchestrator.py:564` 循环头 |
| **M3** | **并发安全**：写工具串行（Step 内 `asyncio.Semaphore(1)`），读工具并发；或每个并发分支独立 session | M | `tool_executor.py:152-203` |
| **L1** | **写工具人机确认**：第一次调用前发 confirm 事件，前端弹确认；Lead 已有 `ask_user` 可复用（`lead_agent_tools.py:127-131`） | L（3~5 天，含前端） | 新增 `tool_policy.py` |
| **L2** | 工具选择治理：>15 时按 intent 分桶注入；加"选错工具"埋点（现在只有 `invalid_tool_streak`，`react_agent.py:289`） | L | 观测 + 分桶 |

### 1.5 事故场景（客户现场会怎么炸）

**场景 A（并发丢块）**：客户说"给我搭个三张图的经营看板"。模型一轮返回 3 个 `add_chart_block`，`gather` 并发（`canvas_orchestrator.py:441`），三个都读到**同一份初始 blocks**，最后写回只剩最后一张 → 画布只有 1 张图，但工具都返回 `ok: true`。用户重试可能又是 2 张。**表现为"随机少图"，不可复现、无法解释**——最伤信任的故障形态。

**场景 B（点了停止还在烧钱）**：客户演示时觉得答得不对，点"停止"。前端没有可调用的 cancel 接口（§1.1），SSE 断开后任务继续跑满步数与查询上限（`MAX_QUERY_CALLS=5`，`react_agent.py:33`）。客户看到的是：浪费 2 分钟 + 一张 token 账单。按调用量计费时这是**可以直接起诉的计费争议**。

**场景 C（提示词诱导 fatal）**：某次模型一轮没调工具，系统注入纠错提示"请立即调用 query_engine / **describe_table** / stats_analyzer"（`agent_orchestrator.py:662`）。模型照做 → `ToolRegistry.get("describe_table")` 返回 `None` → `fatal=True` → 整步终止（`tool_executor.py:175-178` + `agent_orchestrator.py:726-729`）。客户看到"分析到一半突然没了"。

---

## §2 轴 2：权限

### 2.1 权限模型全景（现状，一层一层画出来）

```text
[认证]  JWT Bearer (HS256, sub + exp + type)
        security.py:22-33              <- 无 jti / 无 aud / 无 iss / 无 token 版本
        deps.py:57-81 get_current_user <- 不查 is_active / 不查租户 / 不查密码版本
        x 登出是空操作 (auth.py:87-91)    x 改密不失效旧 token (auth.py:94-110)
         |
[角色]  users.role: admin | editor | viewer   (models/user.py:11-14, 27-29)
        -> 全仓只被用 3 处：
           . 改角色          permissions.py:124
           . 永久删除(trash) trash.py:148
           . 默认 editor     models/user.py:28
        x 数据源/画布/仪表盘/报表/AI 全都不看 role
        = 结论：这不是 RBAC，是「有 role 字段」
         |
[资源归属] 每张业务表一个 user_id，查询侧 filter(user_id == current_user.id)
        datasources / canvases / dashboards / reports / ai_sessions / metrics / insight_rules
        实现相当彻底：query_engine.py:646、dashboard_repository.py:86-99、
        canvases.py:222-296、reports.py:104-183、ai.py:478-514 ...
        v 这一层是全仓做得最好的一层
         |
[共享]  x 没有"共享给某人/某部门"
        v 只有"公开链接"：dashboards/reports.share_token
        x 无有效期（接口现编 30 天，dashboards.py:305 / reports.py:167）
        x 无密码、无次数限制、无撤销、无 deleted_at 过滤（public.py:20,36）
        x 前端 /share/:token 完全免登录（App.tsx:40-41）
         |
[行级/列级数据权限]  x 完全不存在
        没有 policy 表、没有 row filter、没有 column mask 配置
        最接近的是"敏感列"：标记 + 采样脱敏 + 查询拒绝
        sensitive_columns.py 全仓 97 行，只做"名字启发式命中 -> 拒绝查"
```

**一句话**：权限 = **单层属主隔离 + 一个几乎不生效的 role + 一条永久公开链接**。这在"一人一账号的小工具"里够用；在"公司买给 20 个人用"的商品里**不够**——BI 的价值恰恰是"数据要共享给该看的人"，而这里**只有"全给"和"全不给"**。

### 2.2 逐项现状核（file:line）

| 检查项 | 结论 | 证据 |
|---|---|---|
| 认证方式 | JWT Bearer，HS256，`sub`+`exp`+`type`；secret 必填无默认值（好） | `security.py:22-33`、`config.py:41` |
| token 吊销 | 无。`logout` 直接返回成功什么都不做；改密也不失效 | `auth.py:87-91`、`auth.py:94-110` |
| refresh | 只换 access，不轮换 refresh、无黑名单 | `auth.py:74-84` |
| **`get_current_user` 校验了什么** | 只校验：token 可解 + `type=="access"` + user 行存在。**不校验账号状态（模型根本没有 `is_active`）、不校验租户（模型没有 `tenant_id`）、不校验密码版本** | `deps.py:57-81`、`models/user.py:17-36` |
| 租户 | **不存在**。隔离边界 = user_id。后果双向：跨客户共用实例时靠 user 隔离能挡读；但**同公司同事之间也互相看不见** | `models/user.py:17-36` |
| **`api/v1/permissions.py` 实现了什么** | 只有两个接口：① `GET /permissions/users`（**任何登录用户**，含 email/角色/资源计数）；② `PATCH /permissions/users/{id}/role`（**admin only** + 最后一名 admin 防呆）。**没有资源级授权、没有共享、没有部门、没有权限点**——文件名有误导性 | `permissions.py:43-113`、`:116-152` |
| **`api/v1/public.py`** | 免认证；`select(Dashboard).where(share_token==token)`；**不校验 `is_public`、不校验 `deleted_at`、无期限、无密码**；且 `:24` / `:26` 直接访问懒加载关系 → **异步上下文里会抛 `MissingGreenlet`（静态分析高置信，未实测；pytest 无覆盖，只有一次性 live 脚本 `tests/full_auto_test.py:1537-1562`）** | `public.py:15-45`、`models/dashboard.py:36-38`、`models/dashboard_chart.py:35-36` |
| **前端路由守卫** | **只隐藏菜单级别的"假守卫"**：`Boolean(tokenStore.getAccess())` 有 token 就放行；**不看角色、不看过期**；`/permissions` 与 `/audit` 对 viewer 同样可进 | `ProtectedRoute.tsx:5-23`、`App.tsx:45,61-62` |
| token 存放 | `localStorage`（XSS 可取；无 httpOnly cookie 方案） | `api/client.ts:11-35` |
| 注册 | **对外开放、无需邀请**，默认角色 `editor` | `auth.py:54-71`、`models/user.py:27-29` |
| 审计 | HTTP 粒度：`method+path -> action`，记 `user_id/ip/ua/status/duration`；**不记 body** | `middleware.py:82-93`、`operation_log.py:44-81` |
| 审计的 `resource_id` | 从 **path** 里抓第一个 UUID；`POST /ai/query` 的 `datasource_id` 在 body 里 → `resource_id = None` | `middleware.py:45-53`、`ai.py:1371` |
| 凭据 | AES-GCM 封存、响应剔除、不进 DuckDB catalog（`CREATE SECRET`）；`test-connection` 有 SSRF 守卫 + 限流 | `datasource_service.py:286-297`、`schemas/__init__.py:112-135`、`datasources.py:491-523` | **✓ 这块做得好** |

### 2.3 越权测试（只读代码走查，逐条判定）

| # | 攻击面 | 具体路径 | 判定 | 证据 |
|---|---|---|---|---|
| 1 | `datasource_id` | `GET /datasources/{B的id}` / `/preview` / `/tables` / `/disconnect` | **不能** | `datasources.py:106,251,267,189` 全部 `get_by_id(id, current_user.id)` |
| 2 | `canvas_id` | `GET/PATCH/PUT/DELETE /canvases/{B的id}` | **不能** | `canvases.py:256,275,320,516` + `canvas_repository.get_by_id(cid, uid)` |
| 3 | `dashboard_id` | `GET /dashboards/{B的id}` / `/data` / `/share` | **不能** | `dashboards.py:101,212,298` + `dashboard_repository.py:86-93` |
| 4 | `report_id` | `GET /reports/{B的id}` / `/export/pdf` / `/share` | **不能** | `reports.py:104,160,183` |
| 5 | `session_id` | `GET /ai/sessions/{B的id}/messages` | **不能** | `ai.py:478,514,631,653` 均带 `AISession.user_id == current_user.id` |
| 6 | **`?user_id=` 审计** | `GET /audit/logs`（不传 `user_id` 即全量；传别人的也可） | **能（全站可读）** | `audit.py:51,63-65`、docstring `audit.py:62`；导出 `audit.py:181-237` 同权限 |
| 7 | **用户枚举** | `GET /permissions/users` | **能（全员 email + 资源量）** | `permissions.py:43-113`、docstring `:52` |
| 8 | **分享链接** | 拿到 `share_token` 即永久可读；软删除后仍可读 | **能（且无法撤销）** | `public.py:20,36`（无 `deleted_at` / `is_public`）、`dashboard_repository.py:127-140`（只置 `is_public=True`，无 unshare 路径） |
| 9 | **chart_config 挂载** | `POST /dashboards/{我的}` body 里塞**别人的** `chart_config_id` → 再 `GET /dashboards/{我的}` 读回 `queryConfig`（维度/度量/过滤/`datasourceId`） | **能（部分，需先知道 UUID）** | 只校验存在不校验归属：`dashboard_repository.py:160-165`；回吐配置：`dashboards.py:108-119`；数据本身查不出来（`query_engine.py:646` 会挡） |
| 10 | **登出/改密不失效** | 登出后旧 access token 继续用；改密后旧 token 继续用 | **能** | `auth.py:87-91`、`auth.py:107-110`、`deps.py:57-81`（无版本校验） |
| 11 | **注册即入场** | 自注册 → 立刻可调 6、7 两条 → 看全站日志与全员名单 | **能（最危险组合）** | `auth.py:54-71` + 上两条 |
| 12 | **写别人的画布** | 让模型/自己给 `canvas_id` 注入别人的 id（`_inject_context` 允许 LLM 覆盖） | **不能**（下游用 `(cid,uid)` 重查） | `tool_executor.py:136-137` 允许覆盖，但 `canvas_tools.py:229`、`canvas_block_writer.py:73` 兜住 |
| 13 | `metric_id` | `GET /metrics/{B的id}` | **不能**（但 `user_id IS NULL` 的"系统指标"对所有人可见） | `metrics.py:96`（`_visible_scope` 含 `is_(None)`）；是否有租户能在 NULL 下建指标：**未确认** |

**小结**：**"改 id 越权"这条主线是守住的**（属主过滤很彻底，13 条里 8 条不能）。漏的是**"非资源型横向读"**（6、7、11）和**"链接型无期限授权"**（8）——恰好是演示时最容易演示、也最容易被客户安全评审抓到的三类。

### 2.4 对标企业 BI：客户会不会问？

| 能力 | FineBI / QuickBI 的形态 | 我们的状态 | 客户会不会问 |
|---|---|---|---|
| 组织/部门/角色 | 用户→部门→角色→权限组，资源按"目录"授权 | ✗ 只有 user 级 | **会，招标必答项**。699 客户会问"能不能让财务只看财务的表"；2800 客户要"按部门授权" |
| **行级权限**（同表不同行） | 数据集上配 `WHERE 区域 = 当前用户所属区域` | ✗ 无 | **2800 客户 100% 会问**（多分公司/多门店） |
| **列级权限**（同表不同列） | 字段级可见性 + 脱敏（手机号打码） | 半个：敏感列是**拒绝**，不是**脱敏后可用** | **会问**。"工资列能不能只让 HR 看"——我们的答案是"谁都不能看" |
| 数据源可见范围 | 数据源授权给角色/用户组 | ✗ 只有属主可见（同事都看不见，反而挡协作） | 会问："我连的数据源怎么给我同事用？"→ **当前答不了** |
| 分享链接有效期/密码 | 有（天数 + 提取码 + 随时关闭） | ✗ 无（`expiresAt` 是假的，`dashboards.py:305`） | **会问，且是"演示当场就能发现"的那种** |
| 操作审计可回答的问题 | "谁在什么时候看了哪张表哪些列" | ✗ 只能答"谁在什么时候调了哪个 URL" | 合规/金融/医疗客户会问 |
| SSO / 企业微信 / 钉钉 | 标配 | ✗ 只有本地账号 | 2800 客户会问 |

**判断**：
- **699（小团队/单部门）**：会问**分享链接**（第 8 条）和**"我的数据能不能给同事看"**。前者当场翻车，后者是功能缺失。
- **2800（中型企业/多部门）**：一定会问 **行级权限**、**按部门授权**、"审计能不能出报告"。这三条目前**一条都答不了**。

**最小实现（不是做全套 ABAC）**：
1. **共享（M）**：`resource_shares(resource_type, resource_id, grantee_user_id, permission)` 一张表 + 在 5 个 `get_by_id` 里把 `user_id ==` 换成 `user_id == me OR EXISTS(share)`。**这是"能不能给同事看"的唯一答案**。
2. **分享链接治理（S，半天）**：`share_token_expires_at` + `share_password_hash` 两列 + `public.py` 校验 + `DELETE /{res}/{id}/share` 撤销。**做完就把 D3 从"翻车"变成"卖点"**。
3. **角色落到资源（S）**：至少让 `viewer` 不能写（现在 viewer 能建数据源、建画布、跑 AI、发分享链接——因为没有任何地方读 role）。这是"权限模型存在"的最低证明。
4. **行级权限（L）**：数据源上配 `row_policy(datasource_id, field, op, value_expr, role)`，在 `query_engine` 的 WHERE 构造处注入。**只做"按角色注入固定 WHERE"** 就够 80% 场景，不要做表达式引擎。

### 2.5 敏感列与审计（两个必须纠正的认知）

**① "查询结果里真的会脱敏吗"——不会，是"拒绝"。**
- 采样/预览：`mask_rows` 把值换成 `***`（`sensitive_columns.py:91-108`，用于 `datasource_service.py:478-482,515-519`）。
- 查询：**根本不返回**。`query_sql` 用正则查 SQL 里是否出现敏感列名 → 直接 `{"error": "查询包含敏感列..."}`（`agent_tools.py:644-658`）；`query_engine` 在图表配置里查 → `code="SENSITIVE_COLUMN"`（`query_engine.py:734-752`）。
- 所以"脱敏后可用"（手机号打码做用户数、脱敏后看明细）**做不到**；客户要么全禁要么全放。
- 识别是**列名启发式**（`sensitive_columns.py:21-42`）：`cert_no` / `passport` / `cust_id` 不命中；`salary/email/address` 属"弱敏感"**默认不启用**（`:38-42`）。列名改一下（或建视图改名）就绕过 → **这是"提示"，不是"策略"**。
- 另有一处**未确认**的绕过：`query_engine` 对 `expression` 型度量跳过字段校验（`query_engine.py:756-759` 只对 `plain_measures` 做 `_validate_fields`），而敏感检查读的是 `m.get("field")`（`:742`）——指标中心的表达式度量是否能把敏感列带进 SQL，**需实测**。

**② "审计能不能回答谁看了哪张表哪些列"——不能，连"哪张表"都答不了。**
- 记的是 `path`，不是语义：`POST /api/v1/ai/query` → `action="ai.create"`（`operation_log.py:75-76`），`resource_id=None`（`middleware.py:45-53` 只从 path 抓 UUID，而 `datasource_id` 在 body）。
- **不记 body**（`middleware.py:82-93` 只挑 method/path/status/ip/ua）→ SQL、表名、列名、返回行数全部不可见。
- 结论：**"数据访问审计"这个能力不存在**，只有"HTTP 访问审计"。客户问"上周谁导出过客户表"，答不出来。

### 2.6 差距 / 改法 / 事故场景

| 严重度 | 差距 | 改法 | 规模 |
|---|---|---|---|
| **P0** | 审计/用户列表对任何登录用户开放（D1/D2） | `audit.py:47` 与 `permissions.py:43` 加 `if current_user.role != admin: 403`；`user_id` 过滤默认强制为自己 | **S（1 小时）** |
| **P0** | 分享永久有效、不可撤销、软删可读（D3） | 加 `expires_at` / `password_hash` 两列 + `public.py` 校验 + `DELETE /share`；`public.py` 补 `is_public IS TRUE AND deleted_at IS NULL` | **S/M（1~2 天）** |
| **P0** | 注册开放 + 默认 editor（放大 D1/D2） | 加 `ALLOW_REGISTRATION` 开关（默认关），默认角色降 `viewer` | **S** |
| **P1** | 无 token 吊销 | `users.token_version` 进 JWT claim，`get_current_user` 比对；登出/改密 +1 | **S（半天）** |
| **P1** | `role` 不生效 | 写操作统一依赖一个 `require_editor` | **S/M** |
| **P1** | 无共享 | `resource_shares` + 5 处 `get_by_id` 改造 | **M（3~5 天）** |
| **P2** | 无行级权限 | 数据源级 `row_policy` → `query_engine` 注入 WHERE | **L（1~2 周）** |
| **P2** | 审计无数据语义 | 在 `query_sql` / `query_engine` 成功处写 `data_access_log(user, datasource, tables, columns, rows, sql_hash)` | **M** |
| **P2** | 敏感列"全禁"而非"脱敏可用" | 增加 `mask` 模式：检测敏感列 → 查询放行但结果按策略脱敏（复用 `mask_rows`） | **M** |

**事故场景**：

**场景 D（安全评审当场翻车）**：客户 CTO 让安全同事用我们提供的**演示 viewer 账号**试一下。5 分钟内：① 拉到全员邮箱列表（`permissions.py:43`）；② 拉到全站操作日志含 IP（`audit.py:47`）。结论："你们的权限是假的。"——**这两条加起来不到 100 行代码就能修，但不修就是"整个权限模块不可信"**。

**场景 E（分享链接泄露 = 数据泄露事故）**：销售把看板链接发给客户 A 看数据，一年后合作结束，客户 A 的手机里链接还能打开（无期限、无撤销、软删也有效）。若该看板含经营数据，这是**可直接主张的数据泄露**。修复成本 2 天；不修的代价：一次事故。

**场景 F（协作不可用 = 卖不动）**：客户买了 20 个账号。分析师连好数据源、做好看板，想给老板看 → **没有共享功能**，只能发公开链接（永久有效、不可撤销、不能设密码）。老板不想让链接外流 → 只能把老板的账号密码给他登录。**这是"产品不能用于协作"的硬事实**，也是 2800 档客户 5 分钟内会发现的。

---

## §3 卖给别人之前：权限最小可行清单

按"**不做什么会当场翻车**"排序。每条都可独立交付、都可单独演示。

| 序 | 必做项 | 为什么排这个序 | 规模 | 翻车形态 |
|---|---|---|---|---|
| **1** | **关掉 `audit/logs` 与 `permissions/users` 的全员可见**（admin only） | 唯一一条"客户安全同事 5 分钟就能验证"的洞；修它 1 小时 | **S** | 安全评审当场否决整套权限 |
| **2** | **分享链接可设期限、可设密码、可撤销、软删失效** | 唯一一条"销售已经用出去了、但收不回来"的功能；也是能当卖点讲的功能 | **S/M** | 数据泄露事故 + 事后无法止血 |
| **3** | **登出/改密/停用账号能止血**（token 版本或黑名单） | 客户第一反应是"员工离职了怎么办"；现在答"等 30 分钟 token 过期" | **S** | 离职员工/被盗号继续操作，事故定性恶化 |
| **4** | **`role` 落到写操作上**（viewer 只读） | "权限模型存在"的最低证明；现在 viewer 能建数据源、能发分享链接 | **S/M** | 客户问"只读账号能改数据吗"，答"能" |
| **5** | **关闭开放注册**（或改邀请制） | 第 1 条被绕过的另一条路：自注册即拿到全站可见 | **S** | 任何人自助注册即可看全站 |
| **6** | **同事间共享（`resource_shares`）** | 2699/2800 客户"我做的看板怎么给老板看"的唯一正解 | **M** | 产品无法协作 → 只能单机用 |
| **7** | **数据访问审计**（哪张表哪些列、多少行） | 金融/医疗/合规客户必问；也是场景 E 的举证基础 | **M** | 合规审计过不了 |
| **8** | **行级权限（按角色注入固定 WHERE）** | 多分公司/多门店客户的硬需求 | **L** | 招标技术分直接掉档 |
| **9** | 企业微信/钉钉/SSO 登录 | 2800 档的采购流程要求 | **L** | 走不完 IT 审批 |

> 前 5 条加起来约 **3~4 人天**，能把"权限这块是假的"变成"权限这块是个小产品"。第 6、7 条各 3~5 天，是"能不能卖给 20 人团队"的分水岭。第 8、9 条是"能不能进招标"的门槛。

---

## 附：证据索引（按文件）

| 文件 | 关键行 | 用途 |
|---|---|---|
| `services/agents/tool_executor.py` | 39-44, 80-83, 116-117, 131-150, 167-178, 205-251 | 参数解析/无校验/memo/上下文注入/白名单/无超时 |
| `services/agent_tools.py` | 53-56, 285-334, 605-613, 639-667, 703, 1912-1924 | 隐藏集/注册表/归属校验/SQL 超时/50 行截断/工具清单 |
| `services/canvas_tools.py` | 31, 215-235, 404-596, 970-976 | 画布工具/归属重查/50 行/写工具声明 |
| `services/canvas_block_writer.py` | 61-98 | read-modify-write 落库（丢块根因） |
| `services/agents/agent_orchestrator.py` | 94-107, 449, 456, 662, 692-694, 716, 726-729 | 幂等集/并发/超时/幽灵工具/结果压缩 |
| `services/agents/canvas_orchestrator.py` | 39-51, 288-298, 441-452 | 执行器白名单/并发/绕过压缩 |
| `services/agents/react_agent.py` | 32-33, 266-283, 289-323 | 并发上限/查询上限/无效工具处理 |
| `services/agents/lead/lead_agent_tools.py` | 25-31, 89-142 | 写工具硬边界 / 10 工具 |
| `api/deps.py` | 57-81 | get_current_user 只校验存在性 |
| `core/security.py` | 22-41 | JWT 无 jti/版本 |
| `api/v1/auth.py` | 34-51, 54-71, 74-91, 94-110 | 登录限流/开放注册/空登出/改密不失效 |
| `api/v1/permissions.py` | 43-113, 116-152 | 全员可读列表 / 仅此一个真实权限点 |
| `api/v1/audit.py` | 47-133, 181-237 | 审计全员可读 |
| `core/middleware.py` | 45-53, 82-93 | 审计只到 HTTP 粒度、resource_id 取不到 body |
| `core/operation_log.py` | 44-81 | action 由 method+path 推断 |
| `api/v1/public.py` | 15-45 | 分享无鉴权/无期限/无 deleted_at/懒加载 |
| `api/v1/dashboards.py` | 108-119, 150-187, 292-312 | 回吐 chartConfig / 挂载不校验归属 / 假 expiresAt |
| `repositories/dashboard_repository.py` | 22-24, 86-99, 127-140, 160-165 | token 生成 / 属主过滤 / 分享不置期 / 只校验存在 |
| `models/user.py` | 11-14, 17-36 | role 枚举 / 无 tenant / 无 is_active |
| `services/query_engine.py` | 646, 734-752, 756-759 | 数据源属主过滤 / 敏感列拒绝 / expression 度量绕过面 |
| `services/sensitive_columns.py` | 21-42, 91-108, 111-122 | 名字启发式 / 预览脱敏 / SQL 引用检测 |
| `frontend/src/components/layout/ProtectedRoute.tsx` | 5-23 | 仅判 token 存在 |
| `frontend/src/App.tsx` | 40-41, 45, 61-62 | 分享页免登录 / 权限页与审计页对所有人开放 |
