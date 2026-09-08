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
import re
from typing import Any

from app.services.agents.graph import Graph
from app.services.agent_tools import (
    ConversationPhase,
    _PHASE_TOOLS,
    get_tools_for_phase,
)
from app.services.agents.tool_executor import ToolExecutor, build_assistant_message
from app.services.context_utils import compact_result_json

from app.services.llm_client import LLMClient

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6
MAX_CONSECUTIVE_FAILURES = 5  # 连续查询失败熔断阈值
MAX_PARALLEL_TOOL_CALLS = 3   # 单轮并发工具调用上限（防止 LLM 一次性并发大量查询）
MAX_QUERY_CALLS = 5           # 单任务累计查询（query_sql/query_engine）次数上限，超限强制收尾出报告

# 阶段流转触发工具（与 agent_stream 原逻辑一致）
_ANALYZING_TRIGGERS = ("query_sql", "query_engine")
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
                "query_call_count": 0,
                "invalid_tool_streak": 0,
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
            messages, phase_tools, temperature=0.3, max_tokens=3000,
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

        # 输出质量兜底：LLM 即将收尾（无工具调用）时，
        # 1) 完全无文本（静默） → 强制生成收尾报告；
        # 2) 只有"过场话/摘要级"文本（stub）→ 重写为完整报告。
        # 两者都保证终态输出是合格报告，而非空回复或一句话偷懒。
        if not tool_calls:
            text_out = "".join(text_chunks)
            if not text_out.strip():
                logger.warning(f"[react] silent_reason iteration={iteration + 1} 无工具调用且无文本，强制收尾")
                wrapup = await self._run_wrapup_report(messages)
            elif self._looks_like_stub(text_out):
                logger.warning(f"[react] stub_reason iteration={iteration + 1} 文本疑似过场话(len={len(text_out)})，重写报告")
                wrapup = await self._run_wrapup_report(messages)
            else:
                wrapup = ""
            if wrapup:
                for part in self._chunk_text(wrapup):
                    await emit({"type": "text", "content": part})
            elif not text_out.strip():
                await emit({"type": "text", "content": "很抱歉，工具执行后未能生成有效的分析结果，请换一种问法重试。"})

        return {
            "tool_calls": tool_calls,
            "has_text_output": has_text_output,
            "iteration": iteration + 1,
        }

    @staticmethod
    def _looks_like_stub(text: str) -> bool:
        """判断最终文本是不是"过场话/欠报告"：过短、或命中过渡句标记、或缺数据/结构特征。

        用于在 LLM 偷懒收尾（输出'好的，开始分析''已获取数据'之类）时触发重写。
        """
        t = (text or "").strip()
        if not t:
            return False  # 空文本走 silent 分支
        if len(t) < 150:
            return True  # 太短不可能是完整报告
        # 命中过渡/过程话标记 → 疑似偷懒（尤其长度仍偏短时）
        stub_markers = ("开始分析", "已获取", "正在查询", "正在生成", "好的，", "让我", "Let me", "以下将", "接下来")
        if len(t) < 300 and any(m in t for m in stub_markers):
            return True
        # 无数字也无 Markdown 标题 → 缺数据引用与结构，判为 stub
        if not re.search(r"\d", t) and "#" not in t:
            return True
        return False

    @staticmethod
    def _chunk_text(text: str, size: int = 200) -> list[str]:
        """把长文本切成小段，模拟流式输出（便于前端增量渲染）。"""
        return [text[i:i + size] for i in range(0, len(text), size)] if text else []

    async def _run_wrapup_report(self, messages: list[dict]) -> str:
        """强制收尾：用一次无工具 LLM 调用，基于已有消息生成总结报告（降级不抛异常）。"""
        try:
            from app.services.ai_prompts import REPORT_SYSTEM
            report_msg = [{"role": "system", "content": REPORT_SYSTEM}] + [
                m for m in messages if m.get("role") != "system"
            ]
            report = await self.llm.complete(
                report_msg,
                temperature=0.4,
                max_tokens=1500,
            )
            return report if isinstance(report, str) and report.strip() else ""
        except Exception as e:
            logger.warning(f"[react] wrapup_report_failed: {e}")
            return ""

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
        query_call_count = state.get("query_call_count", 0)

        # 单轮并发钳制：防止 LLM 一次性并发大量工具调用（如 8 个查询），
        # 超出部分丢弃（保留前 N 个），收敛为可管理的小步执行。
        original_tool_calls = tool_calls
        if len(tool_calls) > MAX_PARALLEL_TOOL_CALLS:
            logger.warning(f"[react] tool_calls={len(tool_calls)} 超并发上限，截断为 {MAX_PARALLEL_TOOL_CALLS}")
            tool_calls = tool_calls[:MAX_PARALLEL_TOOL_CALLS]

        # 执行层白名单：LLM 可能无视 schema 约束编造当前阶段外的工具调用
        # （如 GENERATING 阶段仍返回 query_sql）。schema 层的过滤只影响 LLM 可见性，
        # 执行层必须在执行前再校验一次，命中白名单外的工具直接剔除。
        phase_allowed = _PHASE_TOOLS.get(phase, set())
        blocked = [tc.get("name") for tc in tool_calls if tc.get("name") not in phase_allowed]
        if blocked:
            logger.warning(f"[react] blocked_tools_out_of_phase phase={getattr(phase, 'value', phase)} blocked={blocked}")
        tool_calls = [tc for tc in tool_calls if tc.get("name") in phase_allowed]
        if not tool_calls:
            # 本轮全部调用都不属于当前阶段 → 不执行任何工具：
            # 先补全 assistant tool_call + tool 拒绝结果（保证消息配对），
            # 再注入纠正并回 reason。若连续多次无效调用（decode 惯性），不再让 LLM 空转，
            # 直接强制 wrapup 收尾报告。
            invalid_tool_streak = state.get("invalid_tool_streak", 0) + 1
            messages.append(build_assistant_message(tool_calls or original_tool_calls))
            for tc in (tool_calls or original_tool_calls):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{len(messages)}"),
                    "content": json.dumps(
                        {"error": f"工具 {tc.get('name')} 在当前阶段不可用"},
                        ensure_ascii=False,
                    ),
                })
            if invalid_tool_streak >= 2:
                logger.warning(f"[react] invalid_tool_streak={invalid_tool_streak} 连续无效调用，强制收尾")
                wrapup = await self._run_wrapup_report(messages)
                if wrapup:
                    for part in self._chunk_text(wrapup):
                        await emit({"type": "text", "content": part})
                else:
                    await emit({"type": "text", "content": "很抱歉，工具执行后未能生成有效的分析结果，请换一种问法重试。"})
                return {
                    "messages": messages,
                    "phase": phase,
                    "executed_tool_names": executed_tool_names,
                    "consecutive_query_failures": consecutive_query_failures,
                    "query_call_count": query_call_count,
                    "invalid_tool_streak": invalid_tool_streak,
                    "done_reason": "invalid_tool_wrapup",
                }
            messages.append({
                "role": "system",
                "content": (
                    f"当前阶段不允许调用工具 {blocked}。"
                    "请：1) 若已完成数据查询，直接输出中文分析报告并调用 render_chart 生成图表；"
                    "2) 不要重复调用查询工具。"
                ),
            })
            logger.info(f"[react] no_allowed_tools phase={getattr(phase, 'value', phase)} 本轮无有效工具 streak={invalid_tool_streak}")
            return {
                "messages": messages,
                "phase": phase,
                "executed_tool_names": executed_tool_names,
                "consecutive_query_failures": consecutive_query_failures,
                "query_call_count": query_call_count,
                "invalid_tool_streak": invalid_tool_streak,
            }

        # 构建 assistant message with tool_calls
        messages.append(build_assistant_message(tool_calls))

        # 共享工具执行器：统一执行/观测/事件 emit/错误判定（react 无幂等 memo）
        executor = ToolExecutor(
            user_id=user_id,
            db_session=db_session,
            emit=emit,
            trace=self.agent_trace,
        )

        has_error = False
        tool_success_count = 0
        tool_error_count = 0
        for tc in tool_calls:
            pr = await executor.execute_tool_call(tc)
            executed_tool_names.append(pr.name)
            logger.info(f"[react] tool_call name={pr.name} result_length={len(pr.result)}")

            # 查询成功/失败计数（Bug C 保护：非 JSON 降级）
            try:
                result_obj = json.loads(pr.result)
            except (json.JSONDecodeError, TypeError):
                result_obj = {"error": f"工具返回了无法解析的结果: {str(pr.result)[:100]}"}
            has_error = "error" in result_obj or pr.fatal
            if has_error:
                tool_error_count += 1
                if pr.name == "query_sql":
                    consecutive_query_failures += 1
                    logger.warning(f"[react] query_failed consecutive={consecutive_query_failures}")
            elif pr.name == "query_sql":
                consecutive_query_failures = 0
                tool_success_count += 1

            messages.append({
                "role": "tool",
                "tool_call_id": pr.tc.get("id", f"call_{len(messages)}"),
                # 防爆：大结果压缩后进上下文（error 结果由 compact_result_json 完整保留供自纠错）
                "content": compact_result_json(pr.result),
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
        if phase == ConversationPhase.SELECTING and not has_error:
            if "list_datasources" in executed_tool_names:
                new_phase = ConversationPhase.ANALYZING
                logger.info("[react] phase_transition SELECTING→ANALYZING")
        elif phase == ConversationPhase.ANALYZING and not has_error:
            if any(n in _ANALYZING_TRIGGERS for n in executed_tool_names):
                new_phase = ConversationPhase.GENERATING
                logger.info("[react] phase_transition ANALYZING→GENERATING")
        elif phase == ConversationPhase.GENERATING:
            if any(n in _GENERATING_TRIGGERS for n in executed_tool_names):
                new_phase = ConversationPhase.REPORTING
                logger.info("[react] phase_transition GENERATING→REPORTING")

        # 累计查询次数上限：query_sql/query_engine 调用 ≥ MAX_QUERY_CALLS 后，
        # 强制收走查询工具（流转到 GENERATING）并提示开始汇总，防止无限逐项查询烧 token。
        # 判定不再依赖 phase（可能已流转 GENERATING，但 LLM 绕过滤限制仍在尝试查询）。
        query_used = sum(1 for n in tool_calls if n in _ANALYZING_TRIGGERS)
        query_call_count += query_used
        query_exhausted = query_call_count >= MAX_QUERY_CALLS and query_used > 0
        if query_exhausted and new_phase == ConversationPhase.ANALYZING:
            new_phase = ConversationPhase.GENERATING
            logger.info(f"[react] query_limit_reached count={query_call_count} force_generating")
        if query_exhausted:
            await emit({"type": "status", "message": f"已到达本任务查询上限（{MAX_QUERY_CALLS} 次），开始汇总出报告"})

        # follow_up 注入：引导 LLM 下一轮输出（不重复塞工具结果，role:tool 已携带完整数据）
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
            "content": follow_up,
        })

        return {
            "messages": messages,
            "phase": new_phase,
            "executed_tool_names": executed_tool_names,
            "consecutive_query_failures": consecutive_query_failures,
            "query_call_count": query_call_count,
            "invalid_tool_streak": 0,
        }

    async def _route_execute(self, state: dict, **shared) -> str:
        return "done" if state.get("done_reason") in ("circuit_breaker", "invalid_tool_wrapup") else "loop"

    # ── follow_up 构建：只告诉 LLM"要干什么"，不出现工具名 ──
    # 工具由当前阶段 tools 参数动态提供，硬编码工具名会让 LLM 在阶段收走工具后
    # 仍"凭记忆"编造调用（如 GENERATING 阶段伪造 query_sql）。
    def _build_follow_up(self, executed_tool_names: list[str], has_error: bool, consecutive_query_failures: int) -> str:
        if executed_tool_names and all(n == "list_datasources" for n in executed_tool_names):
            return (
                "以上是当前可选的数据源。请结合用户的原始问题判断：\n"
                "1. 如果用户问题中已明确提到要分析哪个数据源，直接用当前可用工具继续查询分析，不要停下来询问；\n"
                "2. 如果用户没有明确指定，用友好方式展示数据源列表（名称、类型、关键字段），引导用户选择。\n"
                "使用 ## 标题和列表格式，关键数字加粗。"
            )
        if any(n == "render_chart" for n in executed_tool_names):
            return (
                "图表已生成。现在请根据以上查询结果输出一份中文分析报告，"
                "用 ## 标题分段，关键数字用 **加粗**，用 > 引用块展示重要发现。"
                "**不要再查询了**，直接把报告输出给用户。"
            )
        if any(n == "query_sql" for n in executed_tool_names):
            if has_error:
                if consecutive_query_failures == 1:
                    return (
                        "上次查询失败了。错误提示中已经包含了正确的表引用和可用列名。"
                        "请**直接复制错误提示中的表引用和列名**重写查询，不要自己编表名或列名，然后重试。"
                    )
                if consecutive_query_failures <= 3:
                    return (
                        f"已连续失败 {consecutive_query_failures} 次。请再次确认：\n"
                        "1) 列名是否与错误提示中的可用列名完全一致（区分大小写）；\n"
                        "2) 表引用是否就是错误提示给出的那个；\n"
                        "3) 字符串值是否用了正确的引号。\n"
                        "如果仍然报错，请**最后一次**重试，再失败就把错误信息告诉用户并停止。"
                    )
                return (
                    "已连续失败多次，请停止重试，把最后一次的错误信息用中文告诉用户，"
                    "并建议用户检查数据源连接或简化查询条件。不要再继续重试。"
                )
            return (
                "查询成功！现在请输出一份**完整**的分析报告（不是摘要或过渡语）：\n"
                "1. 先用当前可用工具生成至少一张图表（**必须生成，不允许跳过**）\n"
                "2. 然后输出完整分析报告：结论先行 → 分节详述（每节带具体数值/百分比/排名，"
                "解读趋势与对比）→ 总结论。不少于 300 字\n"
                "**禁止**再发起新的查询，直接用已有数据完成分析；"
                "禁止以'好的，开始分析''已获取数据'等过渡句作为报告内容。"
            )
        return "请根据以上工具执行结果输出回复。用 ## 标题分段，关键数字用 **加粗**。"
