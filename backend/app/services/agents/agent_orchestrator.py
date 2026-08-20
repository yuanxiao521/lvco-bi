"""AgentOrchestrator：多 Agent 协作编排器（骨架规划 + Agentic 执行，LangGraph 模式，零依赖）。
 
多 Agent 协作模式：
    PlannerAgent（规划 Agent）：生成【骨架计划】——只定步骤目标/建议工具/依赖，不写死参数
    ExecutorAgent（执行 Agent）：每个骨架步骤实时决策工具参数并执行，失败 hint 回传重试（≤3 次）
    ReportAgent（报告 Agent）：汇总计划与执行结果生成分析报告
 
图结构：
    plan（PlannerAgent 生成骨架）
      ├─ 条件: 计划有效 → execute_steps（ExecutorAgent 执行非图表步骤）
      ├─ 条件: 计划为空 → report（直接报告）
      └─ 条件: 规划失败 → error
    execute_steps
      ├─ 条件: 计划含 render_chart → chart_steps（ExecutorAgent 执行图表步骤，数据自动填充）
      └─ 条件: 计划无图表 → report
    chart_steps → report
    report / error 为终结点
 
节点通过 emit 回调（asyncio.Queue 桥接）流式输出事件，execute_task 保持 AsyncIterator 接口。
"""
import asyncio
import hashlib
import json
import logging
from typing import Any, AsyncIterator
 
from app.config import settings
from app.services.agents.base_agent import AgentResult
from app.services.agents.planner_agent import PlannerAgent, _infer_table_description
from app.services.agents.graph import Graph
from app.services.agent_tools import ToolRegistry
from app.services.context_utils import compact_result_json
from app.services.llm_client import LLMClient
from app.services.observability import get_observer, observe_tool_call
 
logger = logging.getLogger(__name__)
 
_CHART_TOOL = "render_chart"
_MAX_STEP_RETRIES = 3  # 执行 Agent 单步失败重试上限
 
# Shared state keys (used to avoid LLM prompt injection via key names)
_K_USER_MSG = "user_msg"
_K_STEP_ID = "step_id"
_K_GOAL = "goal"
_K_TOOL = "tool"
_K_ID = "id"
_K_NAME = "name"
_K_TYPE = "type"
_K_EMPTY = "''"
_K_NO_TOOL = "(谢谢选择)"
 
 
def _topo_sort(steps: list[dict]) -> list[dict]:
    """按 depends_on 依赖关系拓扑排序步骤（Kahn 算法）。
    无依赖的步骤保持原顺序；有环时忽略环中后置步骤。
    """
    by_id = {s["step_id"]: s for s in steps}
    deps = {s["step_id"]: set(s.get("depends_on") or []) for s in steps}
    for sid in deps:
        deps[sid] = {d for d in deps[sid] if d in by_id}
    ordered: list[dict] = []
    while deps:
        ready = [sid for sid, d in deps.items() if not d]
        if not ready:
            logger.warning(f"[orchestrator] 计划依赖存在环，忽略环中依赖 remaining={len(deps)}")
            ready = [next(iter(deps))]
            deps[ready[0]] = set()
        ready.sort()
        for sid in ready:
            ordered.append(by_id[sid])
            deps.pop(sid)
            for other in deps:
                deps[other].discard(sid)
    return ordered
 
 
def _rows_to_2d(columns: list, rows: list) -> list:
    """把 query_engine 返回的 dict 行转成 render_chart 需要的二维数组行。"""
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [[r.get(c) for c in columns] for r in rows]
    return rows


# Task 8 (P1-6)：只读幂等工具集合 —— 这些工具的相同参数结果在同一轮（会话内）可被 memo 缓存复用。
_IDEMPOTENT_TOOLS = frozenset({
    "list_datasources",
    "list_tables",
    "get_datasource_info",
    "describe_table",
    "list_fields",
    "analyze_column",
})


# Task 2 (P0-2)：失败签名 key —— 同一工具 + 相同参数（JSON 规范化哈希）判定为同一失败签名。
def _make_fail_key(tool_name: str, args: dict) -> str:
    """生成失败签名：`tool_name:sha1(args)[:8]`。"""
    h = hashlib.sha1(json.dumps(args or {}, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{tool_name}:{h[:8]}"


# Task 8 (P1-6)：memo key，与失败签名同构（工具名 + 参数哈希）。
def _make_memo_key(tool_name: str, args: dict) -> str:
    return _make_fail_key(tool_name, args)


# Task 3 (P0-3)：按 depends_on 依赖关系把步骤分成拓扑层，同层内可并发执行。
def _group_steps_by_level(steps: list[dict]) -> list[list[dict]]:
    """按最长依赖链计算每步层级（无依赖=层级 0），返回按层级升序的步骤分组。"""
    by_id = {s["step_id"]: s for s in steps}
    level_of: dict[int, int] = {}
    remaining = set(by_id)
    while remaining:
        progressed = False
        for sid in list(remaining):
            deps = [d for d in (by_id[sid].get("depends_on") or []) if d in by_id]
            if any(d not in level_of for d in deps):
                continue  # 依赖尚未定级
            level_of[sid] = (max(level_of[d] for d in deps) + 1) if deps else 0
            remaining.discard(sid)
            progressed = True
        if not progressed:  # 存在环或缺失依赖，兜底：其余步骤放入下一层
            for sid in list(remaining):
                deps = [d for d in (by_id[sid].get("depends_on") or []) if d in by_id]
                level_of[sid] = (max((level_of.get(d, -1) for d in deps), default=-1) + 1)
                remaining.discard(sid)
    levels: list[list[dict]] = []
    for s in steps:
        lv = level_of[s["step_id"]]
        while len(levels) <= lv:
            levels.append([])
        levels[lv].append(s)
    return levels


# Task 9 (P2-11)：异常安全的对话历史摘要（简单截断拼接）。
def _summarize_history_safe(history: list[dict] | None, max_chars: int = 2000) -> str:
    if not history:
        return ""
    try:
        parts = []
        for m in history:
            role = str(m.get("role", ""))
            content = str(m.get("content", ""))
            if content.strip():
                parts.append(f"{role}: {content.strip()}")
        joined = "\n".join(parts)
        if len(joined) > max_chars:
            joined = joined[:max_chars] + "..."
        if not joined:
            return ""
        return f"[对话历史摘要]\n{joined}"
    except Exception:
        return "[对话历史摘要]\n（无法解析历史消息）"
 
 
class AgentOrchestrator:
    """多 Agent 协作编排器：PlannerAgent 骨架规划 → ExecutorAgent 逐步执行 → ReportAgent 报告。"""
 
    def __init__(self, llm: LLMClient, db_session, extra_plannable_tools: set[str] | None = None):
        self.llm = llm
        self.db_session = db_session
        # 按入口注入的额外可规划工具（如画布工具）→ 透传给 Planner，白名单 = orchestrator_safe ∪ extra
        self.extra_plannable_tools: set[str] = set(extra_plannable_tools or ())
        self.planner = PlannerAgent(llm, self.extra_plannable_tools)
        self.graph = self._build_graph()
        logger.info("AgentOrchestrator（多 Agent 协作版）初始化完成")
 
    def _build_graph(self) -> Graph:
        """构建协作图：plan →(条件)→ execute_steps →(条件)→ chart_steps → report / error。"""
        g = Graph("orchestrator")
        g.add_node("plan", self._plan_node)
        g.add_node("execute_steps", self._execute_steps_node)
        g.add_node("chart_steps", self._chart_steps_node)
        g.add_node("report", self._report_node)
        g.add_node("error", self._error_node)
        g.set_entry_point("plan")
        g.add_conditional_edges(
            "plan",
            self._route_plan,
            {"ok": "execute_steps", "empty": "report", "error": "error"},
        )
        g.add_conditional_edges(
            "execute_steps",
            self._route_execute,
            {"chart": "chart_steps", "report": "report"},
        )
        g.add_edge("chart_steps", "report")
        g.set_finish_point("report", "error")
        return g
 
    # ── 图节点：规划（PlannerAgent 生成骨架计划）──
    async def _plan_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        await emit({"type": "status", "message": "正在分析需求并生成执行计划..."})
        
        # [DEBUG] 记录 Planner 输入上下文
        logger.debug(
            f"[orchestrator][planner_input]\n"
            f"  user_msg: {shared['user_msg'][:200]}\n"
            f"  history_count: {len(shared['history'])}\n"
            f"  datasources_count: {len(shared['available_datasources'])}\n"
            f"  datasources: {json.dumps(shared['available_datasources'][:3], ensure_ascii=False)[:500]}"
        )
        
        result = await self.planner.execute(
            user_msg=shared["user_msg"],
            history=shared["history"],
            available_datasources=shared["available_datasources"],
        )
        
        # [DEBUG] 记录 Planner 输出
        if result.success:
            logger.debug(
                f"[orchestrator][planner_output]\n"
                f"  task_summary: {result.data.get('task_summary', '')[:200]}\n"
                f"  expected_output: {result.data.get('expected_output', '')}\n"
                f"  steps_count: {len(result.data.get('steps', []))}\n"
                f"  steps: {json.dumps(result.data.get('steps', []), ensure_ascii=False)[:1000]}"
            )
        else:
            logger.debug(f"[orchestrator][planner_failed] error={result.error}")
        
        if not result.success:
            logger.warning(f"[orchestrator] 规划失败，使用智能降级计划: {result.error}")
            fallback_plan = self._build_fallback_plan(shared["user_msg"], shared["available_datasources"])
            steps = fallback_plan.get("steps", [])
            has_chart = any(s.get("tool") == _CHART_TOOL for s in steps)
            logger.info(f"[orchestrator] 降级计划完成 steps={len(steps)} has_chart={has_chart}")
            await emit({"type": "plan", "plan": fallback_plan})
            state_sink = shared.get("state_sink")
            if state_sink is not None:
                state_sink["plan"] = fallback_plan
            return {
                "plan": fallback_plan,
                "has_chart_steps": has_chart,
                "ordered_steps": _topo_sort(steps),
            }
        plan = result.data
        steps = plan.get("steps", [])
        has_chart = any(s.get("tool") == _CHART_TOOL for s in steps)
        logger.info(f"[orchestrator] 骨架规划完成 steps={len(steps)} has_chart={has_chart}")
        await emit({"type": "plan", "plan": plan})
        state_sink = shared.get("state_sink")
        if state_sink is not None:
            state_sink["plan"] = plan
        return {
            "plan": plan,
            "has_chart_steps": has_chart,
            "ordered_steps": _topo_sort(steps),
        }

    def _build_fallback_plan(self, user_msg: str, available_datasources: list[dict]) -> dict:
        """Planner 失败时生成智能降级计划：直接查询数据源。"""
        ds = available_datasources or []
        if ds:
            ds0 = ds[0] if isinstance(ds[0], dict) else {}
            ds_desc = ds0.get("description") or _infer_table_description(ds0)
            fields = ds0.get("fields", [])
            field_parts = []
            for f in (fields[:10] if isinstance(fields, list) else []):
                name = f.get("name", "?")
                dtype = f.get("data_type", "")
                samples = f.get("sample", [])
                sample_str = f" 示例={samples}" if samples else ""
                field_parts.append(f"{name}({dtype}{sample_str})" if dtype else name)
            field_hint = "，字段：" + "、".join(field_parts) if field_parts else ""
            steps = [
                {
                    "step_id": 1,
                    "goal": f"查询数据回答用户问题：{user_msg}。数据源为 {ds0.get('name', 'default')}（{ds_desc}）{field_hint}，请根据字段名生成合适的 SQL 查询",
                    "tool": "query_datasource",
                    "depends_on": [],
                    "purpose": "直接查询数据回答用户问题",
                },
                {
                    "step_id": 2,
                    "goal": f"根据查询结果生成可视化图表，标题与用户问题'{user_msg}'相关",
                    "tool": "render_chart",
                    "depends_on": [1],
                    "purpose": "可视化展示分析结果",
                },
            ]
        else:
            steps = [
                {
                    "step_id": 1,
                    "goal": "列出所有可用数据源，找到包含相关数据的数据源",
                    "tool": "list_datasources",
                    "depends_on": [],
                    "purpose": "无可用数据源信息，先列出数据源",
                },
            ]
        return {"task_summary": f"智能降级计划：{user_msg}", "steps": steps, "expected_output": "report"}
 
    async def _route_plan(self, state: dict, **shared) -> str:
        if state.get("plan_error"):
            return "error"
        steps = (state.get("plan") or {}).get("steps") or []
        return "empty" if not steps else "ok"
 
    # ── 图节点：执行非图表步骤（ExecutorAgent：LLM 实时决策参数 + 失败重试）──
    # Task 3 (P0-3)：同拓扑层内步骤并发执行；Task 11 (P2-9)：emit analyzing 阶段事件。
    async def _execute_steps_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        await emit({"type": "status", "phase": "analyzing", "message": "正在分析并执行数据工具步骤..."})
        results = dict(state.get("results") or {})
        ordered = state.get("ordered_steps") or []
        steps = [s for s in ordered if s.get("tool") != _CHART_TOOL]
        memo = shared.get("tool_memo")
        if memo is None:
            memo = {}
            shared["tool_memo"] = memo
        shared.setdefault("tool_memo_lock", asyncio.Lock())
        levels = _group_steps_by_level(steps)
        for level in levels:
            await asyncio.gather(
                *(self._run_step_with_timeout(step, results, state, **shared) for step in level)
            )
        state_sink = shared.get("state_sink")
        if state_sink is not None:
            state_sink["results"] = dict(results)
        return {"results": results}

    # ── 图节点：执行图表步骤（ExecutorAgent 决策图表类型/标题，数据代码自动填充）──
    # Task 11 (P2-9)：emit generating 阶段事件。图表步骤保持串行以稳定全局 LLM 响应消费次序。
    async def _chart_steps_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        await emit({"type": "status", "phase": "generating", "message": "正在生成可视化图表..."})
        results = dict(state.get("results") or {})
        ordered = state.get("ordered_steps") or []
        for step in ordered:
            if step["tool"] != _CHART_TOOL:
                continue
            await self._run_step_with_timeout(step, results, state, **shared)
        state_sink = shared.get("state_sink")
        if state_sink is not None:
            state_sink["results"] = dict(results)
        return {"results": results}
 
    async def _route_execute(self, state: dict, **shared) -> str:
        has_chart = state.get("has_chart_steps", "MISSING")
        logger.info(f"[orchestrator] _route_execute has_chart={has_chart}")
        return "chart" if has_chart else "report"
 
    # ── Agentic 步骤执行：mini ReAct 循环（LLM 可多次调用工具，直到输出文本或达到上限）──
    _MAX_TOOL_CALLS_PER_STEP = 5  # 单步内最多工具调用次数

    # Task 6 (P1-8)：单步骤超时包装 —— 超时标记 skipped/timeout 并继续整体流程，不中断其他步骤。
    async def _run_step_with_timeout(self, step: dict, results: dict, state: dict, **shared) -> None:
        emit = shared["emit"]
        sid = step["step_id"]
        goal = step.get("goal") or step.get("purpose") or "执行当前步骤"
        timeout = getattr(settings, "AGENT_STEP_TIMEOUT", 30)
        try:
            await asyncio.wait_for(
                self._agentic_run_step(step, results, state, **shared),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timeout_result = json.dumps({
                "skipped": True,
                "timeout": True,
                "goal": goal,
                "skipped_reason": f"步骤 {sid} 执行超时（>{timeout}s），已跳过",
            }, ensure_ascii=False)
            results[sid] = timeout_result
            await emit({"type": "tool_result", "name": goal[:20], "result": timeout_result})
            logger.warning(f"[orchestrator] 步骤 {sid} 执行超时，标记 skipped/timeout")
        except asyncio.CancelledError:
            # 整体超时取消传播至此：不在此吞掉，让取消继续向上冒泡到 wait_for(整体)。
            raise

    async def _agentic_run_step(self, step: dict, results: dict, state: dict, **shared) -> None:
        from app.services.ai_prompts import EXECUTOR_SYSTEM
        emit = shared["emit"]
        db_session = shared.get("db_session")
        user_id = state.get("user_id", "")
        sid = step["step_id"]
        goal = step.get("goal") or step.get("purpose") or "执行当前步骤"

        context = self._build_executor_context(state, step, results, **shared)

        # [DEBUG] 记录 Executor 输入上下文
        logger.debug(
            f"[orchestrator][executor_input] step_id={sid}\n"
            f"  goal: {goal[:200]}\n"
            f"  suggested_tool: {step.get('tool', '')}\n"
            f"  depends_on: {step.get('depends_on', [])}\n"
            f"  context_length: {len(context)}\n"
            f"  context_preview: {context[:500]}"
        )

        messages: list[dict] = [
            {"role": "system", "content": EXECUTOR_SYSTEM},
            {"role": "user", "content": context},
        ]
        all_tools = ToolRegistry.schemas()

        # [DEBUG] 记录可用工具列表
        logger.debug(
            f"[orchestrator][executor_tools] step_id={sid}\n"
            f"  available_tools_count: {len(all_tools)}\n"
            f"  tool_names: {[t.get('function', {}).get('name', '') for t in all_tools]}"
        )

        tool_call_count = 0  # 本步骤内已执行的工具调用次数
        last_result: str = ""  # 最后一次工具执行结果（用于达到上限时兜底）
        reasoning_content: str = ""  # DeepSeek reasoning 模式需要回传
        consecutive_errors = 0  # 连续错误计数
        # Task 2 (P0-2)：同一步骤内按失败签名（工具+参数哈希）记录连续失败次数。
        fail_counts: dict[str, int] = {}

        # ── mini ReAct 循环：LLM 决策 → 执行工具 → 结果回传 → 再决策 ──
        while tool_call_count < self._MAX_TOOL_CALLS_PER_STEP:
            # 1. LLM 决策：调用工具或输出文本
            tool_calls: list[dict] = []
            text_parts: list[str] = []
            try:
                async for event in self.llm.stream_chat_with_tools(
                    messages, all_tools, temperature=0.3, max_tokens=2000,
                ):
                    if event["type"] == "text":
                        text_parts.append(event.get("content", ""))
                    elif event["type"] == "tool_call":
                        # 保存本轮 LLM 返回的 reasoning_content（DeepSeek 需要回传）
                        rc = event.get("reasoning_content", "")
                        if rc:
                            reasoning_content = rc
                        tool_calls.append(event)
            except Exception as e:
                logger.warning(f"[orchestrator] 步骤 {sid} 流式调用异常: {e}")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    logger.error(f"[orchestrator] 步骤 {sid} 连续 3 次异常，放弃")
                    results[sid] = json.dumps({"error": f"连续 3 次 API 调用失败: {e}"}, ensure_ascii=False)
                    return
                # 清除 messages 中可能引起问题的 reasoning_content
                for msg in messages:
                    msg.pop("reasoning_content", None)
                continue

            # [DEBUG] 记录 LLM 决策结果
            logger.debug(
                f"[orchestrator][executor_decision] step_id={sid} tool_call_count={tool_call_count}\n"
                f"  tool_calls_count: {len(tool_calls)}\n"
                f"  text_output_length: {sum(len(t) for t in text_parts)}\n"
                f"  tool_calls: {json.dumps([{'name': tc.get('name', ''), 'args_preview': str(tc.get('arguments', '{}'))[:200]} for tc in tool_calls], ensure_ascii=False)[:500]}"
            )

            # 2a. 无工具调用：文本输出即为步骤结果，结束本步骤
            if not tool_calls:
                text_out = "".join(text_parts)
                results[sid] = json.dumps({"text": text_out}, ensure_ascii=False)
                if text_out.strip():
                    await emit({"type": "tool_result", "name": goal[:20], "result": results[sid]})
                logger.info(f"[orchestrator] 步骤 {sid} 文本输出 len={len(text_out)}")
                logger.debug(f"[orchestrator][executor_no_tool] step_id={sid} text_preview: {text_out[:300]}")
                self._record_step_trace(shared.get("trace"), sid, "text", tool_call_count + 1, False)
                return

            # 2b. 执行工具（一次一个调用）
            tc = tool_calls[0]
            tname = tc.get("name", "")
            try:
                targs = json.loads(tc.get("arguments", "{}") or "{}")
            except json.JSONDecodeError:
                targs = {}
            if tname in (_CHART_TOOL, "stats_analyzer"):
                self._fill_step_data(targs, step, results)
            await emit({"type": "tool_call", "name": tname, "args": targs})

            tool = ToolRegistry.get(tname)
            if tool is None:
                err = json.dumps({"error": f"未知工具: {tname}"}, ensure_ascii=False)
                results[sid] = err
                await emit({"type": "tool_result", "name": tname, "result": err})
                self._record_step_trace(shared.get("trace"), sid, tname, tool_call_count + 1, True)
                return

            # Task 8 (P1-6)：幂等工具结果 memo —— 命中缓存直接复用，不再执行工具。
            is_idempotent = tname in _IDEMPOTENT_TOOLS
            mkey = _make_memo_key(tname, targs) if is_idempotent else None
            memo = shared.get("tool_memo") if is_idempotent else None
            memo_lock = shared.get("tool_memo_lock") if is_idempotent else None

            result_str: str | None = None
            if is_idempotent and memo is not None and memo_lock is not None:
                # 幂等工具：在锁内完成 读-执行-写，保证并发下同一签名仅真实执行一次。
                async with memo_lock:
                    if mkey in memo:
                        cached = memo[mkey]
                        results[sid] = cached
                        await emit({"type": "tool_result", "name": tname, "result": cached, "memo": True})
                        logger.info(f"[orchestrator] 步骤 {sid} 命中 memo tool={tname}")
                        self._record_step_trace(shared.get("trace"), sid, tname, tool_call_count + 1, False)
                        return
                    result_str = await self._execute_tool_once(user_id, db_session, tname, targs, tool, shared)
                    memo[mkey] = result_str
                # 幂等工具成功结果即为该步骤的权威结果，直接结束步骤，避免被后续文本覆盖。
                try:
                    parsed_res = json.loads(result_str)
                    is_exec_error = isinstance(parsed_res, dict) and "error" in parsed_res
                except Exception:
                    is_exec_error = False
                if not is_exec_error:
                    results[sid] = result_str
                    await emit({"type": "tool_result", "name": tname, "result": result_str})
                    self._record_step_trace(shared.get("trace"), sid, tname, tool_call_count + 1, False)
                    return
            else:
                result_str = await self._execute_tool_once(user_id, db_session, tname, targs, tool, shared)

            # 判断工具返回是否含 error
            try:
                parsed_res = json.loads(result_str)
                is_error = isinstance(parsed_res, dict) and "error" in parsed_res
            except Exception:
                is_error = False

            await emit({"type": "tool_result", "name": tname, "result": result_str})

            # [DEBUG] 记录工具执行结果
            logger.debug(
                f"[orchestrator][tool_result] step_id={sid} tool_call_count={tool_call_count}\n"
                f"  tool_name: {tname}\n"
                f"  is_error: {is_error}\n"
                f"  result_length: {len(result_str)}\n"
                f"  result_preview: {result_str[:500]}"
            )

            tool_call_count += 1
            last_result = result_str
            consecutive_errors = 0  # 工具调用成功，重置连续错误计数

            # 3. 把 assistant tool_call + tool result 追加到 messages，让 LLM 看到结果后继续决策
            assistant_msg: dict = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tc.get("id", f"call_{tool_call_count}"),
                    "type": "function",
                    "function": {"name": tname, "arguments": tc.get("arguments", "{}")},
                }],
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            messages.append(assistant_msg)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{tool_call_count}"),
                "content": result_str,
            })

            if is_error:
                logger.warning(f"[orchestrator] 步骤 {sid} 工具返回错误 tool={tname} attempt={tool_call_count}")
                # Task 2 (P0-2)：按失败签名计数；达 2 次注入强制换工具指令，达 3 次跳过步骤。
                fkey = _make_fail_key(tname, targs)
                fails = fail_counts.get(fkey, 0) + 1
                fail_counts[fkey] = fails
                if fails >= 3:
                    reason = f"同一工具与参数连续失败 3 次（{fails}），跳过步骤 {sid}：{tname}"
                    results[sid] = json.dumps({"skipped": True, "skipped_reason": reason}, ensure_ascii=False)
                    await emit({"type": "tool_result", "name": tname, "result": results[sid]})
                    logger.warning(f"[orchestrator] 步骤 {sid} 因 {reason} 跳过")
                    self._record_step_trace(shared.get("trace"), sid, tname, tool_call_count, True)
                    return
                if fails == 2:
                    force_msg = (
                        f"[系统指令] 检测到同一工具与参数连续失败 2 次（工具：{tname}，"
                        f"参数：{json.dumps(targs, ensure_ascii=False)}）。"
                        f"请务必更换工具或修改参数后再尝试，不要再调用工具 {tname}。"
                    )
                    messages.append({"role": "system", "content": force_msg})
                    logger.warning(f"[orchestrator] 步骤 {sid} 注入强制换工具指令 (fails={fails})")
                # 错误时继续循环（含强制换工具指令后），让 LLM 看到错误后修正
                continue

            # 工具成功：如果是 chart 步骤，发 chart 事件
            if tname == _CHART_TOOL:
                try:
                    cr = json.loads(result_str)
                    if cr.get("option"):
                        await emit({"type": "chart", "chart_type": cr.get("chart_type", "bar"), "option": cr["option"]})
                except Exception as ce:
                    logger.warning(f"[orchestrator] chart 事件解析失败: {ce}")

            logger.info(f"[orchestrator] 步骤 {sid} 工具成功 tool={tname} (第{tool_call_count}次调用)")
            # 成功后继续循环，让 LLM 决定是否需要更多工具调用

        # 达到工具调用上限，用最后一次工具结果作为步骤结果
        results[sid] = last_result or json.dumps({"error": "步骤执行失败（超过工具调用上限）"}, ensure_ascii=False)
        logger.warning(f"[orchestrator] 步骤 {sid} 达到工具调用上限 {self._MAX_TOOL_CALLS_PER_STEP}")
        self._record_step_trace(shared.get("trace"), sid, "max_calls", tool_call_count, False)

    async def _execute_tool_once(
        self,
        user_id: str,
        db_session,
        tname: str,
        targs: dict,
        tool,
        shared: dict,
    ) -> str:
        """执行单次工具调用并返回结果字符串（含观测 span 与异常兜底）。"""
        trace = shared.get("trace")
        span_obj = None
        try:
            if trace is not None:
                with observe_tool_call(trace, tname, args=targs) as span:
                    span_obj = span
                    result_str = await tool.execute(user_id=user_id, db_session=db_session, **targs)
            else:
                result_str = await tool.execute(user_id=user_id, db_session=db_session, **targs)
        except Exception as e:
            result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
        if span_obj is not None:
            try:
                parsed_res = json.loads(result_str)
                is_error = isinstance(parsed_res, dict) and "error" in parsed_res
            except Exception:
                is_error = False
            span_obj.update(
                output=result_str[:300],
                metadata={"ok": not is_error, "attempt": 1},
            )
        return result_str

    def _build_executor_context(self, state: dict, step: dict, results: dict, **shared) -> str:
        """构建执行 Agent 上下文：用户问题 + 当前步骤 + 依赖步骤结果 + 数据源信息。"""
        parts: list[str] = []
        parts.append(f"用户问题：{shared.get(_K_USER_MSG, _K_EMPTY)}")
        # Task 9 (P2-11)：注入异常安全的会话历史摘要。
        hist = shared.get("history")
        if hist:
            summary = _summarize_history_safe(hist)
            if summary:
                parts.append("对话历史摘要：" + summary.strip())
        parts.append(f"当前步骤（step_id={step[_K_STEP_ID]}）：目标：{step.get(_K_GOAL, _K_EMPTY)}；建议工具：{step.get(_K_TOOL) or _K_NO_TOOL}")
        prior: list[str] = []
        for dep in step.get("depends_on") or []:
            r = results.get(dep)
            if r:
                prior.append(f"- 步骤{dep}: {compact_result_json(r, 600)}")
        if prior:
            parts.append("已完成的依赖步骤结果：\n" + "\n".join(prior))
        ds = shared.get("available_datasources") or []
        if ds:
            ds_lines = []
            for d in ds[:10]:
                ds_desc = d.get("description") or _infer_table_description(d)
                field_parts = []
                for f in (d.get("fields") or [])[:15]:
                    name = f.get("name", "?")
                    dtype = f.get("data_type", "")
                    samples = f.get("sample", [])
                    sample_str = f" 示例值={samples}" if samples else ""
                    field_parts.append(f"{name}({dtype}{sample_str})" if dtype else name)
                fields = ", ".join(field_parts)
                table_ref = d.get("table_ref", "")
                ds_lines.append(f"- id={d.get(_K_ID)} name={d.get(_K_NAME)} 描述={ds_desc} type={d.get(_K_TYPE)} table_ref={table_ref} 字段: {fields}")
            parts.append("可用数据源（SQL 的 FROM 必须用 table_ref 原样复制）：\n" + "\n".join(ds_lines))
        return "\n\n".join(parts)
 
    # ── 图节点：报告（ReportAgent）／错误 ──
    async def _report_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        # Task 11 (P2-9)：reporting 阶段事件。
        await emit({"type": "status", "phase": "reporting", "message": "正在生成分析报告..."})
        report, source = await self._generate_report(
            shared["user_msg"],
            state.get("plan") or {},
            state.get("results") or {},
            history=shared.get("history"),
        )
        await emit({"type": "text", "content": report, "report_source": source})
        await emit({"type": "done"})
        return {}
 
    async def _error_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        msg = state.get("plan_error") or state.get("__error__") or "任务执行失败"
        await emit({"type": "error", "message": str(msg)})
        return {}
 
    # ── 数据填充与报告 ──
    @staticmethod
    def _record_step_trace(trace, sid: str, tool: str, attempts: int, failed: bool) -> None:
        """把单步执行的尝试次数/重试次数/成败状态写入 trace metadata（观测增强）。

        统计口径：attempts=总尝试次数（含首次），retries=失败后重试次数。
        """
        if trace is None:
            return
        stats = trace.metadata.setdefault("step_stats", {})
        stats[str(sid)] = {
            "tool": tool,
            "attempts": attempts,
            "retries": max(0, attempts - 1),
            "failed": failed,
        }
        trace.metadata["total_steps"] = len(stats)

    def _fill_step_data(self, args: dict, step: dict, results: dict[int, str]) -> None:
        """render_chart/stats_analyzer 缺 columns/rows 时，从依赖步骤的查询结果自动填充。"""
        if args.get("columns") and args.get("rows"):
            return
        for dep in step.get("depends_on") or []:
            dep_result = results.get(dep)
            if not dep_result:
                continue
            try:
                parsed = json.loads(dep_result)
            except Exception:
                continue
            columns = parsed.get("columns")
            rows = parsed.get("rows")
            if isinstance(columns, list) and isinstance(rows, list) and columns and rows:
                args["columns"] = columns
                args["rows"] = _rows_to_2d(columns, rows)
                logger.info(f"[orchestrator] 步骤数据已从步骤 {dep} 自动填充 columns={len(columns)}")
                return
 
    async def _generate_report(
        self,
        user_msg: str,
        plan: dict,
        results: dict[int, str],
        history: list[dict] | None = None,
    ) -> tuple[str, str]:
        """基于计划与各步骤执行结果，生成最终中文分析报告。

        返回 `(report, report_source)`：source 为 "llm"（LLM 成功）或
        "template"（异常/空响应时降级为结构化模板报告）。
        """
        try:
            prompt = self._build_report_prompt(user_msg, plan, results, history=history)
            response = await self.llm.complete(prompt, temperature=0.4, max_tokens=1500)
            if not (response or "").strip():
                raise ValueError("LLM 返回空报告，降级模板")
            return response, "llm"
        except Exception as e:
            logger.error(f"生成报告失败，降级模板报告: {e}")
            task_summary = (plan or {}).get("task_summary") or ""
            steps_info = self._build_steps_info(plan, results)
            return self._generate_template_report(task_summary, steps_info), "template"

    # Task 1 (P0-4)：模板化报告 —— 不依赖 LLM，直接根据计划与步骤结果生成结构化 Markdown。
    def _build_steps_info(self, plan: dict, results: dict) -> list[dict]:
        """把计划步骤与执行结果规整为 `{step_id, goal, result}` 列表（按拓扑序）。"""
        steps_info: list[dict] = []
        ordered = _topo_sort(list((plan or {}).get("steps") or []))
        for s in ordered:
            steps_info.append({
                "step_id": s["step_id"],
                "goal": s.get("goal") or s.get("purpose") or s.get("tool") or "",
                "result": (results or {}).get(s["step_id"], ""),
            })
        return steps_info

    @staticmethod
    def _infer_step_status(result_str: str) -> str:
        if not result_str or not result_str.strip():
            return "未执行"
        try:
            parsed = json.loads(result_str)
        except Exception:
            return "成功"
        if isinstance(parsed, dict):
            if parsed.get("skipped") and parsed.get("timeout"):
                return "超时"
            if parsed.get("skipped"):
                return "跳过"
            if "error" in parsed:
                return "失败"
        return "成功"

    @staticmethod
    def _format_step_detail(result_str: str) -> str:
        """步骤结果的简明摘要（数据表前 5 行 / 图表说明 / 文本，暂无则不输出）。"""
        if not result_str or not result_str.strip():
            return ""
        try:
            parsed = json.loads(result_str)
        except Exception:
            return str(result_str)[:300]
        if not isinstance(parsed, dict):
            return str(result_str)[:300]
        if parsed.get("text"):
            return str(parsed["text"])[:300]
        if "chart_type" in parsed or "option" in parsed:
            return f"已生成图表（类型：{parsed.get('chart_type', 'chart')}）"
        if "columns" in parsed or "rows" in parsed:
            columns = parsed.get("columns") or []
            rows = parsed.get("rows") or []
            out: list[str] = []
            for row in rows[:5]:
                if isinstance(row, list):
                    cells = [f"{columns[i]}={row[i]}" for i in range(min(len(columns), len(row)))]
                elif isinstance(row, dict):
                    cells = [f"{c}={row.get(c)}" for c in columns]
                else:
                    cells = [str(row)]
                out.append("- " + ", ".join(cells))
            out.append(f"... 共 {len(rows)} 行" if len(rows) > 5 else f"共 {len(rows)} 行")
            return "\n".join(out)
        if "error" in parsed or parsed.get("skipped"):
            return ""
        return str(result_str)[:300]

    def _generate_template_report(self, task_summary: str, steps_info: list[dict]) -> str:
        """生成纯模板的降级报告：标题 + 各步骤状态/摘要 + 综合说明，零 LLM 依赖。"""
        title = (task_summary or "").strip() or "任务执行报告"
        lines: list[str] = [f"# {title}", ""]
        for info in steps_info or []:
            sid = info.get("step_id", "?")
            goal = info.get("goal") or info.get("name") or "未命名步骤"
            result_str = info.get("result") or ""
            lines.append(f"步骤 {sid}：{goal}")
            lines.append(f"状态：{self._infer_step_status(result_str)}")
            detail = self._format_step_detail(result_str)
            if detail:
                lines.append(detail)
            lines.append("")
        lines.append("以上为系统自动生成的结构化报告。")
        return "\n".join(lines)

    def _build_report_prompt(
        self,
        user_msg: str,
        plan: dict,
        results: dict[int, str],
        history: list[dict] | None = None,
    ) -> list[dict[str, str]]:
        """构建报告提示词。"""
        steps_info = []
        for s in plan.get("steps", []):
            sid = s["step_id"]
            purpose = s.get("purpose") or s.get("goal") or ""
            result_str = results.get(sid, "")
            try:
                parsed = json.loads(result_str) if result_str else {}
                if "error" in parsed:
                    summary = f"[失败] {str(parsed.get('error'))[:200]}；hint: {str(parsed.get('hint', ''))[:100]}"
                elif "summary" in parsed:
                    summary = json.dumps(parsed.get("summary"), ensure_ascii=False)[:500]
                elif "suggestions" in parsed or "insights" in parsed:
                    summary = result_str[:800]
                else:
                    summary = result_str[:800]
            except Exception:
                summary = str(result_str)[:500]
            steps_info.append(f"步骤{sid} [{s.get('tool') or 'text'}] {purpose}: {summary}")
 
        system_prompt = """你是一个数据分析专家（ReportAgent）。根据用户的查询需求、执行计划和工具执行结果，生成一份清晰的中文分析报告。
 
要求：
1. 用简洁语言总结分析结果，突出关键数据和发现
2. 若某步骤失败，如实说明失败原因（引用工具返回的错误/hint），并给出可操作建议
3. 使用 Markdown 格式：## 标题分段、**加粗**关键数字、> 引用块展示重要发现
4. 引用真实数据，不编造
"""
 
        # Task 9 (P2-11)：注入对话历史摘要。
        history_block = ""
        if history:
            summary = _summarize_history_safe(history)
            if summary:
                history_block = f"对话历史摘要：\n{summary}\n\n"
        user_prompt = (
            f"{history_block}用户问题: {user_msg}\n\n"
            f"执行计划: {json.dumps(plan, ensure_ascii=False)}\n\n"
            f"执行结果:\n" + "\n".join(steps_info) + "\n\n请生成分析报告。"
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
 
    # ── 流式入口 ──
    async def execute_task(
        self,
        user_msg: str,
        history: list[dict],
        user_id: str,
        available_datasources: list[dict],
        **kwargs,
    ) -> AsyncIterator[dict]:
        """执行用户任务：骨架规划 → ExecutorAgent 逐步执行 → 报告，流式产出事件。
 
        事件类型：status / plan / tool_call / tool_result / chart / text / done / error
        """
        queue: asyncio.Queue = asyncio.Queue()
 
        async def emit(ev: dict) -> None:
            await queue.put(ev)
 
        async def run() -> None:
            state_sink: dict = {}
            try:
                observer = get_observer()
                with observer.trace(
                    "orchestrator_execute",
                    user_id=user_id,
                    session_id=getattr(self.db_session, "session_id", None) if self.db_session else None,
                    metadata={"user_msg_length": len(user_msg), "history_length": len(history or [])},
                ) as trace:
                    # Task 6 (P1-8)：整体执行包超时控制，超时后取消并输出已完成结果的模板报告。
                    await asyncio.wait_for(
                        self.graph.invoke(
                            {"user_id": user_id},
                            user_msg=user_msg,
                            history=history or [],
                            available_datasources=available_datasources,
                            db_session=self.db_session,
                            emit=emit,
                            trace=trace,
                            state_sink=state_sink,
                        ),
                        timeout=settings.AGENT_ORCHESTRATOR_TIMEOUT,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[orchestrator] 整体执行超时（>{settings.AGENT_ORCHESTRATOR_TIMEOUT}s），"
                    f"输出已完成步骤的模板报告"
                )
                plan = state_sink.get("plan") or {}
                results = state_sink.get("results") or {}
                task_summary = plan.get("task_summary") or ""
                steps_info = self._build_steps_info(plan, results)
                report = self._generate_template_report(task_summary, steps_info)
                await emit({"type": "text", "content": report, "report_source": "template"})
                await emit({"type": "done", "timeout": True})
            except Exception as e:
                logger.exception(f"[orchestrator] 图执行异常: {e}")
                await emit({"type": "error", "message": f"执行异常: {str(e)}"})
            finally:
                await queue.put(None)
 
        task = asyncio.create_task(run())
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await task