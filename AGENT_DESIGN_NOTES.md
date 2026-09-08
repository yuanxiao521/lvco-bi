# LvcoBI Agent 架构沉淀笔记

> 用途：秋招面试谈资 + 未来改造索引。只记录**亲手实现/亲手验证**的内容。
> 更新：2026-09-08

---

## 1. Agent 双路径架构与防线

```
请求 → L1/L2 校验 → 路由分类器（LLM，json_object）
                      ├─ simple  → ReAct 单 Agent（轻量快速）
                      └─ complex → 编排器（Planner 骨架 → Executor 逐步 → Report）
```

### 1.1 路由分类器踩坑（重要经验）
- **现象**：复杂任务永远走 simple（ReAct），从不进编排器
- **根因**：分类器 `max_tokens=20` + deepseek **思考模式**先消耗 tokens →
  `reasoning_content` 吃光配额，`content` 返回空 → 解析失败 → 默认 simple
- **修复**：`enable_thinking=False`（LLMClient 透传）+ `max_tokens=200` + 空内容防御 +
  超时 2s→8s
- **教训**：llm 返回空 ≠ 调用失败，日志无报错时会伪装成"正常判定"；轻量判断任务
  关思考模式
- **面试话术**："路由分类器是纯指令任务，不该用思考模型；max_tokens 必须覆盖
  reasoning+content 两部分"

### 1.2 ReAct 防线（react_agent.py）
| 机制 | 常量/阈值 | 作用 |
|------|----------|------|
| 迭代上限 | MAX_ITERATIONS=6 | 防死循环 |
| 单轮并发钳制 | MAX_PARALLEL_TOOL_CALLS=3 | 防一次并发 8 查询 |
| 执行层白名单 | `_PHASE_TOOLS[phase]` | 拦"LLM 无视 schema 编造工具名" |
| 连续失败熔断 | MAX_CONSECUTIVE_FAILURES=5 | 连败终止 |
| 空输出兜底 | — | LLM 无工具无文本 → 强制 complete 报告 |
| 无效调用强制收尾 | streak≥2 | 编造工具被拦 2 次 → wrapup 不再空转 |

**核心认知**：LLM 可能会**编造不在 tools 列表里的工具调用**（deepseek 对 tools 参数
约束宽松 + 从历史消息"惯性延续"工具名）。schema 层过滤只影响"LLM 可见性"，
**执行层必须二次校验**。

### 1.3 编排器防线（agent_orchestrator.py）
- 单步工具上限 `_MAX_TOOL_CALLS_PER_STEP=3`
- 全局 LLM 预算**动态计算**：`1(planner) + 步骤数×3 + 1(report) + 2(余量)`，
  替代固定 12（复杂任务 5-6 步实测打满 12 被误杀）
- 同工具+同参数失败签名：2 次注入强制换工具，3 次跳过步骤
- 整体超时 60s→180s（思考模式 LLM 单轮 10-20s，60s 常触发模板报告降级）

---

## 2. 表现型问题与根因（实战排查）

### 2.1 "查询很多次还出不了图 / 静默收尾"
- **链**：LLM 并发 8 查询 → 迭代上限耗尽被强制 done → 无最终文本 → 空回复
- **根**：react 路径原本没有"终态必有报告"的兜底（orchestrator 有 `_report_node`）
- **修**：空输出兜底 + 无效调用强制收尾

### 2.2 同一 SQL 反复查（趋势查 6 次）
- **根**：工具结果被压缩（样本 10 行）→ LLM 看到"截断"→ 不信任 `summary.rows_count`
  → **恐慌性重查**（换 LIMIT/去 LIMIT，其实拿同一批数据）
- **对治**：工具返回标注"聚合完整 / 明细样本+全量数"（见第 5 节），
  杜绝"以为没查全"
- **教训**：LLM 对"截断展示"有数据不全恐惧，宁可查多次不读 summary

### 2.3 render_chart 空参数（`args={}`）
- **根**：流式 function-calling 逐 token 生成，模型可能只打出工具名、arguments 没继续
- **修**：execute 显式校验缺参 + 返回带枚举的引导性错误 + description 强调必填
- **教训**：协议允许 arguments 为空字符串，必填参数要靠 execute 侧校验兜底

---

## 3. 缓存全景（全项目 6 处）

| # | 缓存 | 存储 | Key | TTL | 失效 | 作用域 |
|---|------|------|-----|-----|------|--------|
| 1 | query_engine | Redis(降级内存) | `query:{ds}:{user}:{config_hash}` | 全局 300s | 数据源变更 | **跨任务共享** |
| 2 | ToolExecutor memo | 内存 dict | `tool:sha1(args)[:8]` | 任务结束即清 | — | **仅幂等元数据工具**（list_* 等 6 个） |
| 3 | 仪表盘 | Redis | `dashboard:{id}:data` | 看板 refresh_interval | 看板变更 | 按看板 |
| 4 | derived metric | 内存 | `context["_cache"]` | 单次调用 | — | 递归防护 |
| 5 | 压缩记忆 | PostgreSQL `ai_memories` | 按 session 一行 | 永久 | upsert | 跨轮对话 |
| 6 | prompt YAML | 进程内 | 按文件 | mtime 热加载 | 文件变更 | 常驻 |

**关键结论**：
- 唯一"编排周期内"缓存 = ToolExecutor memo，但**不覆盖查询工具**
- query_engine 缓存要求 config 完全一致，LLM 参数微差即 miss → 实际命中率低
- 静态数据下无名害；动态数据下缺 fresh 逃生口 + 陈数据风险

**记忆机制**（短期/长期）：
- 短期：`ai_messages` 全量 DB 直读 → `compress_history(keep=60, 15 万字符)`
- 长期：`smart_compress_history`（≥3 轮触发）→ LLM 摘要 ≤200 字 → `ai_memories` upsert
  → 下轮 `【压缩摘要】` 注入开头（COMPRESSION_MARKER 链式累积）
- **无 Redis/内存缓存**，纯 DB。评估：个人项目低并发+本地 DB 不是瓶颈，**不引入缓存**
  （面试话术：不为不存在的瓶颈设计；真要优化先走增量加载而非缓存层）

---

## 4. 数据粒度 × 缓存一致性设计（讨论结论，部分未实现）

**"全量 vs 样本"四层落点**：工具层规则（免费）> route 扩展（意义小）> **Planner 骨架带
granularity（推荐）** > 独立意图模块（重叠，不推荐）

**动态数据挑战链**：缓存过期（陈数据）→ 多步骤快照不一致（报告不自洽）→
粒度×新鲜度错配

**成熟方案**：`fresh` 参数 / TTL 分级 / 快照隔离（DuckDB 单任务=单连接视角）/
新鲜度标注

**分层设计（面试答案）**：
```
无条件 memo（元数据，永远幂等）
→ 按静态性 memo（查询，静态源可缓存）
→ 全局 TTL 缓存（query_engine）
→ 显式 fresh（动态实时场景）
```

**memo 不扩展查询的原因**：查询非幂等（动态下同 SQL 结果不同）+ 错误会被缓存放大 +
与 Redis 职责重叠 + 掩盖恐慌重查（治标）

---

## 5. 滞留改造清单（2026-09-08 起）

- [x] query_engine 返回加 `cached: true/false` + 数据时间戳（探测痕迹）
- [x] 工具返回标注"聚合完整 / 明细样本+全量数"（治恐慌重查）
- [x] planner 骨架预留 `granularity` 字段位（先不加逻辑）
- [ ] （未来）query_sql 任务级 memo + 静态源判断
- [ ] （未来）fresh 参数 / TTL 分级 / 快照隔离
- [ ] （未来）画布助手真实 E2E