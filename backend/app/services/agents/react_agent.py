"""ReactGraphAgent：ReAct 循环的图化实现（LangGraph 模式，零依赖）。

图结构：
    reason（LLM 推理：流式调用 + 按 phase 过滤工具 schema）
      ├─ 条件: 有 tool_calls 且未超迭代上限 → execute_tools
      └─ 条件: 无 tool_calls（最终文本回复/无输出）→ done
    execute_tools（执行工具：emit 事件 + 熔断 + phase 流转 + follow_up 注入）
      ├─ 条件: 熔断触发 → done
      └─ 无条件 → reason（循环边）
    done 为终结点

与 agent_stream 原 ReAct 循环行为等价，但以图引擎表达（节点/边/条件路由/共享 State）。
"""
import asyncio
import json
import logging
from typing import Any

from app.services.agents.graph import Graph
from app.services.agent_tools import ToolRegistry, ConversationPhase, get_tools_for_phase
from app.services.context_utils import compact_result_json
from app.services.observability import observe_tool_call
from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6
MAX_CONSECUTIVE_FAILURES = 5  # 连续查询失败熔断阈值

# 阶段流转触发工具（与 agent_stream 原逻辑一致）
_ANALYZING_TRIGGERS = ("query_datasource", "query_engine")
_GENERATING_TRIGGERS = ("render_chart",)


class ReactGraphAgent:
    """ReAct 图化 Agent：reason（LLM 推理）→ execute_tools（工具执行）循环，直至最终回复。"""

    def __init__(self, llm: LLMClient, all_tools: list[dict], agent_trace=None):
        self.llm = llm
        self.all_tools = all_tools
        self.agent_trace = agent_trace  # 可观测 span 容器（可为 None）
        self.graph = self._build_graph()

    def _build_graph(self) -> Graph:
        g = Graph("react")
        g.add_node("reason", self._reason_node)
        g.add_node("execute_tools", self._execute_tools_node)
        g.set_entry_point("reason")
        g.add_conditional_edges("reason", self._route_reason, {"execute": "execute_tools", "done": "done"})
        g.add_conditional_edges("execute_tools", self._route_execute, {"loop": "reason", "done": "done"})
        g.set_finish_point("done")
        return g

    async def run(
        self,
        messages: list[dict],
        user_id: str,
        db_session,
        initial_phase: str = "selecting",
        emit=None,
    ) -> dict:
        """执行 ReAct 图，返回最终 state。

        emit: 异步事件回调（接收 tool_call/tool_result/chart/text 等事件），None 时静默。
        """
        async def _noop(ev):
            pass
        emit_fn = emit or _noop
        final_state = await self.graph.invoke(
            {
                "messages": messages,
                "user_id": user_id,
                "phase": ConversationPhase(initial_phase) if isinstance(initial_phase, str) else initial_phase,
                "executed_tool_names": [],
                "consecutive_query_failures": 0,
                "iteration": 0,
            },
            db_session=db_session,
            emit=emit_fn,
        )
        # 观测：把汇总统计写入 trace metadata（迭代数/工具执行/熔断原因）
        if self.agent_trace is not None:
            self.agent_trace.metadata.update({
                "iterations": final_state.get("iteration", 0),
                "executed_tool_names": final_state.get("executed_tool_names") or [],
                "consecutive_query_failures": final_state.get("consecutive_query_failures", 0),
                "done_reason": final_state.get("done_reason") or "final_answer",
                "final_phase": getattr(final_state.get("phase"), "value", str(final_state.get("phase", ""))),
            })
        return final_state

    # ── 节点 1：LLM 推理 ──
    async def _reason_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        messages = state["messages"]
        phase = state["phase"]
        iteration = state.get("iteration", 0)
        phase_tools = get_tools_for_phase(phase, self.all_tools)

        llm_span = None
        if self.agent_trace is not None:
            llm_span = self.agent_trace.span(name=f"agent_iter_{iteration}", span_type="generation")
            llm_span.input = {"iteration": iteration, "phase": getattr(phase, "value", phase), "messages_count": len(messages)}

        tool_calls: list[dict] = []
        text_chunks: list[str] = []
        has_text_output = False
        async for event in self.llm.stream_chat_with_tools(
            messages, phase_tools, temperature=0.5, max_tokens=3000,
        ):
            if event["type"] == "text":
                has_text_output = True
                text_chunks.append(event.get("content", ""))
                await emit(event)
            elif event["type"] == "tool_call":
                tool_calls.append(event)

        if llm_span is not None:
            llm_span.update(
                output={
                    "text_length": sum(len(c) for c in text_chunks),
                    "tool_calls": [{"name": t.get("name")} for t in tool_calls],
                    "has_text_output": has_text_output,
                },
            )
            llm_span.finish()

        return {
            "tool_calls": tool_calls,
            "has_text_output": has_text_output,
            "iteration": iteration + 1,
        }

    async def _route_reason(self, state: dict, **shared) -> str:
        if state.get("tool_calls") and state.get("iteration", 0) <= MAX_ITERATIONS:
            return "execute"
        return "done"

    # ── 节点 2：执行工具 ──
    async def _execute_tools_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        db_session = shared.get("db_session")
        user_id = state["user_id"]
        messages = state["messages"]
        tool_calls = state.get("tool_calls") or []
        phase = state["phase"]
        executed_tool_names = list(state.get("executed_tool_names") or [])
        consecutive_query_failures = state.get("consecutive_query_failures", 0)

        # 构建 assistant message with tool_calls
        assistant_tool_calls = []
        for tc in tool_calls:
            assistant_tool_calls.append({
                "id": tc.get("id", f"call_{len(assistant_tool_calls)}"),
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc.get("arguments", "{}"),
                },
            })
        messages.append({"role": "assistant", "content": None, "tool_calls": assistant_tool_calls})

        all_results: list[dict] = []
        has_error = False
        tool_success_count = 0
        tool_error_count = 0
        for tc in tool_calls:
            tname = tc.get("name", "")
            targs_str = tc.get("arguments", "{}")
            try:
                targs = json.loads(targs_str) if targs_str else {}
            except json.JSONDecodeError:
                targs = {}
            executed_tool_names.append(tname)
            logger.info(f"[react] tool_call name={tname}")
            await emit({"type": "tool_call", "name": tname, "args": targs})

            tool = ToolRegistry.get(tname)
            if tool:
                try:
                    if self.agent_trace is not None:
                        with observe_tool_call(self.agent_trace, tname, args=targs) as span:
                            result = await tool.execute(user_id=user_id, db_session=db_session, **targs)
                            span.update(output=result[:300])
                    else:
                        result = await tool.execute(user_id=user_id, db_session=db_session, **targs)
                    all_results.append({"name": tname, "result": result})
                    logger.info(f"[react] tool_success name={tname} result_length={len(result)}")
                    await emit({"type": "tool_result", "name": tname, "result": result})

                    # 查询成功/失败计数（Bug C 保护：非 JSON 降级）
                    try:
                        result_obj = json.loads(result)
                    except (json.JSONDecodeError, TypeError):
                        result_obj = {"error": f"工具返回了无法解析的结果: {str(result)[:100]}"}
                    has_error = "error" in result_obj
                    if has_error:
                        tool_error_count += 1
                        if tname == "query_datasource":
                            consecutive_query_failures += 1
                            logger.warning(f"[react] query_failed consecutive={consecutive_query_failures}")
                    elif tname == "query_datasource":
                        consecutive_query_failures = 0
                        tool_success_count += 1

                    # chart 事件（render_chart 内嵌校验，无人工确认）
                    if tname == "render_chart":
                        try:
                            cr = json.loads(result)
                            if cr.get("option"):
                                logger.info(f"[react] chart_generated type={cr.get('chart_type')}")
                                await emit({"type": "chart", "chart_type": cr.get("chart_type", "bar"), "option": cr["option"]})
                        except Exception as ce:
                            logger.warning(f"[react] render_chart_parse_failed error={ce}")
                except Exception as e:
                    logger.error(f"[react] tool_execution_failed name={tname} error={e}")
                    err = json.dumps({"error": str(e)}, ensure_ascii=False)
                    all_results.append({"name": tname, "result": err})
                    await emit({"type": "tool_result", "name": tname, "result": err})
                    if tname == "query_datasource":
                        consecutive_query_failures += 1
                    has_error = True
                    tool_error_count += 1
            else:
                logger.error(f"[react] unknown_tool name={tname}")
                err = json.dumps({"error": f"未知工具: {tname}"}, ensure_ascii=False)
                all_results.append({"name": tname, "result": err})
                await emit({"type": "tool_result", "name": tname, "result": err})
                has_error = True
                tool_error_count += 1

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{len(messages)}"),
                "content": all_results[-1]["result"],
            })

        # 熔断：连续查询失败超过阈值，终止循环
        if consecutive_query_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.error(f"[react] circuit_breaker triggered failures={consecutive_query_failures}")
            await emit({"type": "text", "content": (
                f"\n\n> 抱歉，连续 {consecutive_query_failures} 次查询都失败了。"
                "可能是数据源字段名或表结构与我预期的不一致。"
                "请检查数据源是否正确连接，或尝试用更简单的查询方式。"
            )})
            return {
                "messages": messages,
                "phase": phase,
                "executed_tool_names": executed_tool_names,
                "consecutive_query_failures": consecutive_query_failures,
                "done_reason": "circuit_breaker",
            }

        # Phase 状态流转（与 agent_stream 原逻辑一致）
        new_phase = phase
        if phase == ConversationPhase.ANALYZING and not has_error:
            if any(n in _ANALYZING_TRIGGERS for n in executed_tool_names):
                new_phase = ConversationPhase.GENERATING
                logger.info("[react] phase_transition ANALYZING→GENERATING")
        elif phase == ConversationPhase.GENERATING:
            if any(n in _GENERATING_TRIGGERS for n in executed_tool_names):
                new_phase = ConversationPhase.REPORTING
                logger.info("[react] phase_transition GENERATING→REPORTING")

        # follow_up 注入：引导 LLM 下一轮输出（成功结果压缩，错误完整保留）
        compacted = [
            {"name": r["name"], "result": compact_result_json(r["result"], 1200)}
            for r in all_results
        ]
        results_text = json.dumps(compacted, ensure_ascii=False)
        follow_up = self._build_follow_up(executed_tool_names, has_error, consecutive_query_failures)

        if self.agent_trace is not None:
            self.agent_trace.metadata.update({
                "tool_success_count": tool_success_count,
                "tool_error_count": tool_error_count,
                "tool_calls_in_turn": len(tool_calls),
                "consecutive_query_failures": consecutive_query_failures,
            })
        messages.append({
            "role": "user",
            "content": f"以上工具已执行完毕，结果如下：\n{results_text}\n\n{follow_up}",
        })

        return {
            "messages": messages,
            "phase": new_phase,
            "executed_tool_names": executed_tool_names,
            "consecutive_query_failures": consecutive_query_failures,
        }

    async def _route_execute(self, state: dict, **shared) -> str:
        return "done" if state.get("done_reason") == "circuit_breaker" else "loop"

    # ── follow_up 构建（与 agent_stream 原逻辑一致）──
    def _build_follow_up(self, executed_tool_names: list[str], has_error: bool, consecutive_query_failures: int) -> str:
        if executed_tool_names and all(n == "list_datasources" for n in executed_tool_names):
            return (
                "以上是当前可用的数据源列表。请用友好的方式向用户展示有哪些数据源可用，"
                "每个数据源列出名称、类型和关键字段，引导用户告诉你他想分析哪个数据源。"
                "使用 ## 标题和列表格式，数字加粗。不要索要字段信息（你已经有了）。"
            )
        if any(n == "render_chart" for n in executed_tool_names):
            return (
                "图表已生成。现在请根据以上查询结果输出一份中文分析报告，"
                "用 ## 标题分段，关键数字用 **加粗**，用 > 引用块展示重要发现。"
                "**不要再查询了**，直接把报告输出给用户。"
            )
        if any(n == "query_datasource" for n in executed_tool_names):
            if has_error:
                if consecutive_query_failures == 1:
                    return (
                        "上次查询失败了。错误提示中已经包含了正确的 table_ref 和可用列名。"
                        "请**直接用错误提示中的 table_ref 作为 FROM 子句**，用错误提示中的列名拼写 SQL，不要自己编表名或列名。"
                        "然后调用 query_datasource 重试。"
                    )
                if consecutive_query_failures <= 3:
                    return (
                        f"已连续失败 {consecutive_query_failures} 次。请再次确认：\n"
                        "1) 列名是否完全等于 list_datasources 返回的 columns 数组中的字符串（区分大小写）；\n"
                        "2) FROM 子句是否就是 list_datasources 返回的 table_ref 原样复制；\n"
                        "3) 字符串值是否用了正确的引号。\n"
                        "如果仍然报错，请**最后一次**重试，再失败就把错误信息告诉用户并停止。"
                    )
                return (
                    "已连续失败多次，请停止重试，把最后一次的错误信息用中文告诉用户，"
                    "并建议用户检查数据源连接或简化查询条件。不要再继续重试。"
                )
            return (
                "查询成功！现在请**立即**输出分析结果：\n"
                "1. 先调用 render_chart 生成图表（**必须生成，不允许跳过**）\n"
                "2. 然后输出中文分析报告，用 ## 标题分段，关键数字用 **加粗**\n"
                "**禁止**再发起新的查询，直接用已有数据完成分析。"
            )
        return "请根据以上工具执行结果输出回复。用 ## 标题分段，关键数字用 **加粗**。"
