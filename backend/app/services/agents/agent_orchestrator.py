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
import json
import logging
from typing import Any, AsyncIterator
 
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
 
 
class AgentOrchestrator:
    """多 Agent 协作编排器：PlannerAgent 骨架规划 → ExecutorAgent 逐步执行 → ReportAgent 报告。"""
 
    def __init__(self, llm: LLMClient, db_session):
        self.llm = llm
        self.db_session = db_session
        self.planner = PlannerAgent(llm)
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
    async def _execute_steps_node(self, state: dict, **shared) -> dict:
        results = dict(state.get("results") or {})
        ordered = state.get("ordered_steps") or []
        for step in ordered:
            if step["tool"] == _CHART_TOOL:
                continue  # 图表步骤由 chart_steps 节点执行
            await self._agentic_run_step(step, results, state, **shared)
        return {"results": results}
 
    # ── 图节点：执行图表步骤（ExecutorAgent 决策图表类型/标题，数据代码自动填充）──
    async def _chart_steps_node(self, state: dict, **shared) -> dict:
        results = dict(state.get("results") or {})
        ordered = state.get("ordered_steps") or []
        for step in ordered:
            if step["tool"] != _CHART_TOOL:
                continue
            await self._agentic_run_step(step, results, state, **shared)
        return {"results": results}
 
    async def _route_execute(self, state: dict, **shared) -> str:
        has_chart = state.get("has_chart_steps", "MISSING")
        logger.info(f"[orchestrator] _route_execute has_chart={has_chart}")
        return "chart" if has_chart else "report"
 
    # ── Agentic 步骤执行：mini ReAct 循环（LLM 可多次调用工具，直到输出文本或达到上限）──
    _MAX_TOOL_CALLS_PER_STEP = 5  # 单步内最多工具调用次数

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

            if span_obj is not None:
                span_obj.update(
                    output=result_str[:300],
                    metadata={"ok": not is_error, "attempt": tool_call_count},
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
                # 错误时继续循环，让 LLM 看到错误后修正（不直接 return）
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
 
    def _build_executor_context(self, state: dict, step: dict, results: dict, **shared) -> str:
        """构建执行 Agent 上下文：用户问题 + 当前步骤 + 依赖步骤结果 + 数据源信息。"""
        parts: list[str] = []
        parts.append(f"用户问题：{shared.get(_K_USER_MSG, _K_EMPTY)}")
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
        await emit({"type": "status", "message": "正在生成分析报告..."})
        report = await self._generate_report(
            shared["user_msg"],
            state.get("plan") or {},
            state.get("results") or {},
        )
        await emit({"type": "text", "content": report})
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
    ) -> str:
        """基于计划与各步骤执行结果，生成最终中文分析报告。"""
        try:
            prompt = self._build_report_prompt(user_msg, plan, results)
            response = await self.llm.complete(prompt, temperature=0.4, max_tokens=1500)
            return response
        except Exception as e:
            logger.error(f"生成报告失败: {e}")
            return "报告生成失败，请稍后重试。"
 
    def _build_report_prompt(
        self,
        user_msg: str,
        plan: dict,
        results: dict[int, str],
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
 
        user_prompt = (
            f"用户问题: {user_msg}\n\n"
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
            try:
                observer = get_observer()
                with observer.trace(
                    "orchestrator_execute",
                    user_id=user_id,
                    session_id=getattr(self.db_session, "session_id", None) if self.db_session else None,
                    metadata={"user_msg_length": len(user_msg), "history_length": len(history or [])},
                ) as trace:
                    await self.graph.invoke(
                        {"user_id": user_id},
                        user_msg=user_msg,
                        history=history or [],
                        available_datasources=available_datasources,
                        db_session=self.db_session,
                        emit=emit,
                        trace=trace,
                    )
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