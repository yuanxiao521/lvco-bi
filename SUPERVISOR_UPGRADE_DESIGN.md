# SUPERVISOR 升级设计 — 主导 Agent 从「单次委托」到「主管循环」

> 状态：**设计稿（未实现）** · 2026-09-10
> 相关代码：`backend/app/services/agents/lead/lead_agent.py`、`lead_decider.py`、`lead_tools.py`
> 关联文档：`AGENT_DESIGN_NOTES.md`

---

## 1. 背景与目标

### 1.1 现状（已读代码确认）

`LeadAgent.stream()` 目前是**一条直线**（`lead_agent.py` L93-L181）：

```
intent → decision（1 次）→ 按决策选 1 个分支执行
        ├─ call_analysis / canvas_op → run_analysis（整包委托，一次）
        ├─ ask_user → 反问
        └─ answer → 直答
→ memory 回流 → done
```

- **决策粒度 = 1 次**：委托给 `run_analysis` 后，LeadAgent 全程不干预。
- `run_analysis`（`lead_tools.py` L158）是**调度壳**，按 `mode` 分派执行内核：
  - `canvas` → `CanvasOrchestrator`
  - `orchestrator`（对话默认，写死）→ `AgentOrchestrator`（Planner→Executor→Report）
  - `react` → `ReactGraphAgent`（仅调用方显式传才走，实际基本不触发）
- **没有"简单/复杂"动态路由**：`lead_decider.py` 决策只产出 `tool_args={goal}`，不含 mode；对话一律走复杂编排。

### 1.2 目标（要成的样子）

升级为 **Supervisor（主管循环）** 模式：

```
意图识别（仅第 1 轮，定主目标）
  → 每轮：Supervisor 决策
       ├─ 派发子任务（分析 / 画布 / 问答）→ 执行 → 拿结果摘要
       └─ 看结果，决定下一步或 stop
  → stop / 轮次上限 → 记忆回流 → done
```

**区别一句话**：决策从「1 次」变「N 次」，主管全程在场、按每轮结果协调；执行引擎（三内核）与底层零件不变。

---

## 2. 设计

### 2.1 状态机（每轮循环）

```
[入口] ① 意图识别 classify_intent（仅 round 0）
    ↓
[循环] ② Supervisor 决策 decide_supervisor
    ├─ call_analysis（派发分析/画布子任务，执行内核由 entry+复杂度决定）
    ├─ ask_user   → 反问输出，本轮结束（等用户下一轮消息带回目标）
    ├─ answer     → 直答输出（是否继续由下一轮决策判定，或随 stop 收尾）
    └─ stop       → 退出循环
    ↓
[出口] ④ 记忆回流 _maybe_summarize → ⑤ done(degraded)
```

### 2.2 Supervisor 决策输入（升级点）

`decide_supervisor` 的 prompt 输入从「digest（长期记忆+活记忆）」扩展为四路：

| 输入 | 来源 | 作用 |
|------|------|------|
| 主目标 | round 0 的 user_msg（全程固定） | 防止跑偏 |
| 长期记忆 | `ctx.history_summary`（ai_memories） | 跨会话背景 |
| 活记忆 | `ctx.turns`（最近 N 轮） | 本轮上下文 |
| **子任务结果摘要** | **每轮 `run_analysis` 产出 → `ctx.turn_summaries`** | 主管知道"上一轮干了啥、还缺啥" |

（`_run_analysis_branch` 已有把 progress/结果写入 `turn_summaries` 的机制，直接复用/增强。）

### 2.3 动作集合

`ActionType`：`ANSWER / CALL_ANALYSIS / ASK_USER / STOP`（**无 CANVAS_OP**——执行器选择不归 Supervisor 管，收拢到确定性函数 `_resolve_subtask_mode`）。

| 动作 | 执行 | 轮次影响 |
|------|------|---------|
| call_analysis | run_analysis（执行内核由 entry+复杂度决定，见 2.3.1） | round+1 |
| ask_user | 反问文本 | 本轮结束，等用户 |
| answer | 直答 / LLM 生成 | 可继续或随 stop 收尾 |
| stop | 无 | 退出循环 |

### 2.3.1 执行器选择（确定性，不经过 Supervisor）

| 场景 | mode | 执行内核 |
|------|------|---------|
| 画布入口（entry=canvas） | canvas | CanvasOrchestrator |
| 对话入口 + 复杂任务 | orchestrator | AgentOrchestrator |
| 对话入口 + 简单任务 | react | ReactGraphAgent |
| 显式 constraints.mode | 按显式值 | 按调用方指定 |

复杂度判定：**由 Supervisor 决策顺带输出 `complexity` 字段**（`simple` / `complex`），不再单独调用旧路由分类器——省一次 LLM 调用。`Decision.complexity` 缺省/非法时按意图确定性兜底（analysis/needs_plan → complex，否则 simple）。

### 2.4 收敛与防失控（三闸）

1. `STOP` 动作（主管自主收尾）
2. **轮次上限** `LEAD_MAX_SUPERVISOR_ROUNDS = 4`（新配置项，config.py 增加）
3. **决策失败兜底**：决策超时/解析失败 → 用已有子任务结果直接 `report` 收尾（不空转）
   （预算兜底已由 run_analysis 内部控制：查询上限 5 次、编排单步工具上限等）

### 2.5 伪代码

```python
async def stream(self, user_msg, *, ctx, db_session):
    # round 0：意图只做一次
    intent = await classify_intent(user_msg, history_summary=ctx.history_summary, ...)
    yield intent_event
    goal = user_msg
    ctx.add_turn("user", user_msg)

    for rnd in range(settings.LEAD_MAX_SUPERVISOR_ROUNDS):
        decision = await decide_supervisor(
            goal=goal,
            intent=intent,                    # 后续轮次可复用或轻量重判
            digest=ctx.digest(),              # 长期 + 活记忆
            subtask_summaries=ctx.turn_summaries,   # ← 新增：上轮子任务摘要
            available_datasources=...,        # 决策可用数据源
            degradation=degradation,
        )
        yield decision_event(rnd)

        if decision.action == ActionType.STOP:
            break
        if decision.action == ActionType.ASK_USER:
            async for ev in self._emit_answer(decision.direct_text or "请补充信息"): yield ev
            return
        if decision.action in (ActionType.CALL_ANALYSIS, ActionType.CANVAS_OP):
            async for ev in self._run_analysis_branch(user_msg, decision, ctx, db_session, ds):
                yield ev
            # _run_analysis_branch 已把结果摘要写入 ctx.turn_summaries → 喂给下一轮
        else:  # ANSWER
            async for ev in self._answer_branch(user_msg, decision, ctx): yield ev

    # 出口不变
    memory_event = await self._maybe_summarize(ctx)
    if memory_event:
        yield memory_saved; yield compressed_history
    yield done(degraded)
```

### 2.6 事件协议增量（后端→前端）

- `decision` / `progress` 事件新增字段 `round: int`（前端工作台按轮分组展示，可选）
- 其余事件（intent/tool_call/tool_result/text/report/done…）**字段不变**，前端其余逻辑零改动

### 2.7 后端文件改动清单（预估）

| 文件 | 改动 |
|------|------|
| `lead_decider.py` | `ActionType` 增 `STOP`；决策 prompt 增加子任务摘要输入与 stop 指令；新增 `decide_supervisor`（或扩展现有 `decide_action` 入参） |
| `lead_agent.py` | `stream()` 改为循环（2.5 伪代码）；`_run_analysis_branch` 强化结果摘要回写 |
| `config.py` | 新增 `LEAD_MAX_SUPERVISOR_ROUNDS=4` |
| `lead_tools.py` | 基本不动（run_analysis 保持调度壳） |
| `ai.py` | SSE 出口给 decision/progress 透传 round（两处：chat/stream、canvas/chat 映射段） |

---

## 3. 边界与风险

| 风险 | 对策 |
|------|------|
| 主管循环烧 token | 轮次上限 4 + answer 后倾向收尾 + 决策关思考模式（复用 1.1 踩坑经验） |
| 子任务重复执行（同目标反复 analyze） | 决策 prompt 强调"已有结果摘要时禁止重复查询"；必要时做子任务目标 hash 幂等（如 run_analysis memo 扩展） |
| 多轮结果上下文膨胀 | `digest()` 已有 4000 字裁剪；turn_summaries 只存摘要 |
| 前端工作台"多轮"识别 | 按 round 字段分组展示，未改版本兼容（round 缺省=0） |

## 4. 验收标准（实现后）

1. 一条"先查数据→再落画布"的复合任务：Supervisor 分 ≥2 轮派发，且第二轮能引用第一轮结果（不打回重查）
2. 简单问答（reply/闲聊）：第一轮即 stop，行为与现在一致（回归保护）
3. 轮次超上限：强制收尾出 report，不空转、不抛错
4. 决策失败（LLM 超时/空返回）：降级为已有结果 report，用户有输出
5. 断流/切路由：沿用现有"两段落库 + store 保活"，无回归

## 5. 实施顺序（后续 A 阶段）

1. config 加轮次上限 → 2. `ActionType.STOP` + 决策 prompt 升级 → 3. `stream()` 改循环 → 4. SSE 透传 round → 5. 回归测试（上述验收 2/3/4）+ 人工复合任务验证