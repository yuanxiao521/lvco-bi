"""CanvasOrchestrator：画布专用编排器（报告骨架 → 落块执行 → 简短总结）。

与 AgentOrchestrator 的关系：**编排策略独立**，仅复用底层设施。
- 独立：Planner 用画布版 prompt（canvas_planner_system）规划"报告骨架"
  （h1 标题 / 章节图表 / 叙事），执行器用画布版 prompt（canvas_executor_system）
  逐步调用 add_text_block / add_chart_block 落块；结束时不生成整篇文字报告
  （报告由画布块承载），输出简短总结。
- 复用：Graph 图引擎、ToolExecutor 执行内核、模块级辅助（拓扑排序/分层/失败签名/历史摘要）。

设计取舍：
- add_chart_block 后端自验证取数，因此骨架里不需要单独的 query_sql 步骤；
- 执行阶段工具集**再次收紧**：入口注入的白名单（CANVAS_ALLOWED_TOOL_NAMES）
  含查询类工具（query_engine/query_sql/list_*），那是 Planner/入口用的；
  但执行器（Executor）只暴露纯落块工具（add_chart_block 自带取数验证，
  不再需要裸查数工具），避免 LLM"先查数据却不建图"（实践踩坑：执行步骤
  反复调 query_engine 拿数据而不调 add_chart_block，导致图表/叙事块缺失）；
- 步骤依赖（depends_on）控制叙事/章节顺序，同层并发执行。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from app.services.agent_tools import ToolRegistry
from app.services.agents.agent_orchestrator import (
    _group_steps_by_level,
    _make_fail_key,
    _summarize_history_safe,
    _topo_sort,
)
from app.services.agents.graph import Graph
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.tool_executor import ToolExecutor, build_assistant_message

logger = logging.getLogger(__name__)

_MAX_TOOL_CALLS_PER_STEP = 4  # 单步内最多工具调用次数
_STEP_TIMEOUT = 45  # 单步超时（秒）
_MAX_STEP_FAILURES = 3  # 同一工具+参数连续失败跳过阈值

# 执行器可见工具：仅纯落块工具。查询/取数由 add_chart_block 内部完成，
# 不再让 LLM 在"建图"步骤里先 query_engine 再落块（会查而不建）。
_EXECUTOR_TOOL_NAMES = frozenset({
    "add_chart_block", "add_text_block",
    "update_chart_block", "remove_block", "arrange_layout",
})


class CanvasOrchestrator:
    """画布编排器：报告骨架规划 → 逐步落块 → 简短总结，流式产出事件。"""

    def __init__(self, llm, db_session, extra_plannable_tools: set[str] | None = None):
        from app.services.ai_prompts import CANVAS_EXECUTOR_SYSTEM, CANVAS_PLANNER_SYSTEM

        self.llm = llm
        self.db_session = db_session
        # 严格白名单：由画布入口注入（CANVAS_ALLOWED_TOOL_NAMES）。空集时 Executor 无工具可用。
        self.extra_plannable_tools: set[str] = set(extra_plannable_tools or ())
        self.planner = PlannerAgent(llm, self.extra_plannable_tools, system_prompt=CANVAS_PLANNER_SYSTEM)
        self._executor_system = CANVAS_EXECUTOR_SYSTEM
        self.graph = self._build_graph()

    def _build_graph(self) -> Graph:
        """图结构：plan（报告骨架）→ execute_steps（落块）→ finish（总结）。

        不需要普通编排的 chart_steps / report 节点：落块即图表，报告由画布块呈现。
        """
        g = Graph("canvas_orchestrator")
        g.add_node("plan", self._plan_node)
        g.add_node("execute_steps", self._execute_steps_node)
        g.add_node("finish", self._finish_node)
        g.set_entry_point("plan")
        g.add_conditional_edges("plan", self._route_plan, {"ok": "execute_steps", "done": "finish"})
        g.add_edge("execute_steps", "finish")
        g.set_finish_point("finish")
        return g

    async def execute_task(
        self,
        user_msg: str,
        history: list[dict],
        user_id: str,
        available_datasources: list[dict],
        **kwargs,
    ) -> AsyncIterator[dict]:
        """流式执行：报告骨架规划 → 落块执行 → 总结。

        事件类型：status / plan / tool_call / tool_result / text / done / error
        """
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict) -> None:
            await queue.put(ev)

        async def run() -> None:
            state_sink: dict = {}
            try:
                await self.graph.invoke(
                    {"user_id": user_id},
                    user_msg=user_msg,
                    history=history or [],
                    available_datasources=available_datasources or [],
                    db_session=self.db_session,
                    emit=emit,
                    state_sink=state_sink,
                )
            except Exception as e:
                logger.exception("[canvas_orchestrator] 图执行异常: %s", e)
                await emit({"type": "error", "message": f"画布编排执行异常: {str(e)}"})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await task

    # ── 节点 1：规划报告骨架 ──
    async def _plan_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        await emit({"type": "status", "message": "正在规划报告骨架..."})
        user_msg = shared["user_msg"]
        result = await self.planner.execute(
            user_msg=user_msg,
            history=shared["history"],
            available_datasources=shared["available_datasources"],
        )
        if not result.success:
            logger.warning("[canvas_orchestrator] 规划失败，使用降级计划: %s", result.error)
            plan = self._build_fallback_plan(user_msg, shared["available_datasources"])
        else:
            plan = result.data
        steps = plan.get("steps", [])
        logger.info("[canvas_orchestrator] 报告骨架完成 steps=%d", len(steps))
        await emit({"type": "plan", "plan": plan})
        shared["state_sink"]["plan"] = plan
        return {"plan": plan, "ordered_steps": _topo_sort(steps)}

    @staticmethod
    def _build_fallback_plan(user_msg: str, available_datasources: list[dict]) -> dict:
        """Planner 失败时的降级计划：h1 标题 + 核心图表 + 叙事。"""
        steps = [
            {
                "step_id": 1,
                "goal": "写报告大标题（与用户需求相关的中文标题）",
                "tool": "add_text_block",
                "depends_on": [],
                "purpose": "报告标题",
            },
            {
                "step_id": 2,
                "goal": f"根据数据源新增一张最能回答用户问题的图表：{user_msg[:80]}",
                "tool": "add_chart_block",
                "depends_on": [],
                "purpose": "核心图表",
            },
            {
                "step_id": 3,
                "goal": "写一段简短叙事，引用图表中的关键数字结论",
                "tool": "add_text_block",
                "depends_on": [2],
                "purpose": "结论叙事",
            },
        ]
        return {
            "task_summary": f"画布报告：{user_msg[:50]}",
            "steps": steps,
            "expected_output": "report",
        }

    async def _route_plan(self, state: dict, **shared) -> str:
        steps = (state.get("plan") or {}).get("steps") or []
        return "ok" if steps else "done"

    # ── 节点 2：执行落块步骤（同层并发） ──
    async def _execute_steps_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        await emit({"type": "status", "message": "正在画布上落块..."})
        results = dict(state.get("results") or {})
        ordered = state.get("ordered_steps") or []
        shared.setdefault("tool_memo", {})
        shared.setdefault("tool_memo_locks", {})
        levels = _group_steps_by_level(ordered)
        for level in levels:
            await asyncio.gather(
                *(self._run_step_with_timeout(step, results, state, **shared) for step in level)
            )
        shared["state_sink"]["results"] = dict(results)
        return {"results": results}

    async def _run_step_with_timeout(self, step: dict, results: dict, state: dict, **shared) -> None:
        emit = shared["emit"]
        sid = step["step_id"]
        goal = step.get("goal") or step.get("purpose") or "执行当前步骤"
        try:
            await asyncio.wait_for(
                self._agentic_run_step(step, results, state, **shared),
                timeout=_STEP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            results[sid] = json.dumps({
                "skipped": True, "timeout": True, "goal": goal[:30],
            }, ensure_ascii=False)
            await emit({"type": "tool_result", "name": goal[:20], "result": results[sid]})
            logger.warning("[canvas_orchestrator] 步骤 %s 执行超时", sid)
        except asyncio.CancelledError:
            raise

    async def _agentic_run_step(self, step: dict, results: dict, state: dict, **shared) -> None:
        """单步 mini-ReAct：LLM 决策落块工具 → ToolExecutor 执行 → 直到 goal 完成。"""
        emit = shared["emit"]
        db_session = shared.get("db_session")
        user_id = state.get("user_id", "")
        sid = step["step_id"]
        goal = step.get("goal") or step.get("purpose") or "执行当前步骤"

        executor = ToolExecutor(
            user_id=user_id,
            db_session=db_session,
            emit=emit,
            memo=shared.get("tool_memo"),
            memo_locks=shared.get("tool_memo_locks"),
            allowed_tools=self.extra_plannable_tools or None,
        )

        context = self._build_executor_context(state, step, results, **shared)
        messages: list[dict] = [
            {"role": "system", "content": self._executor_system},
            {"role": "user", "content": context},
        ]
        all_tools = self._executor_tools()

        tool_call_count = 0
        fail_counts: dict[str, int] = {}
        consecutive_errors = 0

        while tool_call_count < _MAX_TOOL_CALLS_PER_STEP:
            # 1. LLM 决策
            tool_calls: list[dict] = []
            text_parts: list[str] = []
            try:
                async for event in self.llm.stream_chat_with_tools(
                    messages, all_tools, temperature=0.3, max_tokens=2000,
                ):
                    if event["type"] == "text":
                        text_parts.append(event.get("content", ""))
                    elif event["type"] == "tool_call":
                        tool_calls.append(event)
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    results[sid] = json.dumps({"error": f"连续 3 次 API 调用失败: {e}"}, ensure_ascii=False)
                    return
                for msg in messages:
                    msg.pop("reasoning_content", None)
                continue

            # 2a. 无工具调用：文本输出即为步骤结果
            if not tool_calls:
                results[sid] = json.dumps({"text": "".join(text_parts)}, ensure_ascii=False)
                return

            # 2b. 并行执行工具（ToolExecutor 统一执行/观测/白名单校验/emit）
            messages.append(build_assistant_message(tool_calls))
            parallel_results = await asyncio.gather(
                *(executor.execute_tool_call(tc) for tc in tool_calls)
            )

            # 3. 结果回传 + 失败签名
            any_fatal = any(pr.fatal for pr in parallel_results)
            for i, pr in enumerate(parallel_results):
                messages.append({
                    "role": "tool",
                    "tool_call_id": pr.tc.get("id", f"call_{tool_call_count + i}"),
                    "content": pr.result,
                })
                if pr.is_error and not pr.fatal:
                    fkey = _make_fail_key(pr.name, pr.args)
                    fails = fail_counts.get(fkey, 0) + 1
                    fail_counts[fkey] = fails
                    if fails >= _MAX_STEP_FAILURES:
                        results[sid] = json.dumps({
                            "skipped": True,
                            "skipped_reason": f"同一工具与参数连续失败 {fails} 次，跳过步骤 {sid}: {pr.name}",
                        }, ensure_ascii=False)
                        return

            tool_call_count += len(tool_calls)
            if any_fatal:
                results[sid] = next(pr.result for pr in parallel_results if pr.fatal)
                return
            # 4. 全部成功：记录最后结果并结束本步骤（落块动作已 emit canvas_action）
            results[sid] = parallel_results[-1].result
            logger.info("[canvas_orchestrator] 步骤 %s 落块成功", sid)
            return

        if tool_call_count > 0:
            results[sid] = parallel_results[-1].result
        else:
            results[sid] = json.dumps({"error": "步骤执行失败（超过工具调用上限）"}, ensure_ascii=False)

    def _executor_tools(self) -> list[dict]:
        """Executor 可见工具 schema：执行阶段只暴露纯落块工具（入口白名单 ∩ 落块工具集）。

        查数/分析类工具（query_engine/query_sql/list_*）对执行器不可见：
        add_chart_block 自带取数验证，裸查工具只会诱导 LLM"取数但不建图"。
        """
        if not self.extra_plannable_tools:
            return []
        allowed = set(self.extra_plannable_tools) & _EXECUTOR_TOOL_NAMES
        if not allowed:
            return []
        return [
            t for t in ToolRegistry.schemas()
            if isinstance(t, dict)
            and isinstance(t.get("function"), dict)
            and t["function"].get("name") in allowed
        ]

    def _build_executor_context(self, state: dict, step: dict, results: dict, **shared) -> str:
        """构建执行 Agent 上下文：用户问题 + 当前步骤 + 依赖结果 + 数据源信息。"""
        parts: list[str] = []
        parts.append(f"用户问题：{shared.get('user_msg', '')}")
        hist = shared.get("history")
        if hist:
            summary = _summarize_history_safe(hist)
            if summary:
                parts.append(summary)
        parts.append(
            f"当前步骤（step_id={step['step_id']}）：目标：{step.get('goal', '')}；"
            f"建议工具：{step.get('tool') or '（无）'}"
        )
        prior: list[str] = []
        for dep in (step.get("depends_on") or []):
            r = results.get(dep)
            if r:
                prior.append(f"- 步骤{dep}: {self._compact_result(r, 600)}")
        if prior:
            parts.append("已完成的依赖步骤结果：\n" + "\n".join(prior))
        ds = shared.get("available_datasources") or []
        if ds:
            ds_lines = []
            for d in ds[:10]:
                fields = d.get("fields") or []
                field_parts = [
                    f"{f.get('name', '?')}({f.get('data_type', '')})"
                    for f in (fields[:15] if isinstance(fields, list) else [])
                ]
                ds_lines.append(
                    f"- id={d.get('id')} name={d.get('name')} type={d.get('type')} "
                    f"table_ref={d.get('table_ref', '')} 字段: {', '.join(field_parts)}"
                )
            parts.append("可用数据源（add_chart_block 的 dimensions/measures 必须用这里的真实字段名）：\n" + "\n".join(ds_lines))
        return "\n\n".join(parts)

    @staticmethod
    def _compact_result(result_str: str, max_chars: int) -> str:
        """依赖步骤结果的简明摘要（图表数据 summary / 错误 / 截断文本）。"""
        try:
            parsed = json.loads(result_str)
        except Exception:
            return str(result_str)[:max_chars]
        if isinstance(parsed, dict):
            if "summary" in parsed:
                return json.dumps(parsed["summary"], ensure_ascii=False)[:max_chars]
            if "error" in parsed:
                return f"[失败] {str(parsed['error'])[:200]}"
        return str(result_str)[:max_chars]

    # ── 节点 3：收尾总结 ──
    async def _finish_node(self, state: dict, **shared) -> dict:
        emit = shared["emit"]
        results = state.get("results") or {}
        block_count = sum(
            1 for r in results.values()
            if isinstance(r, str) and "canvas_action" in r
        )
        await emit({"type": "status", "message": "报告已生成"})
        await emit({
            "type": "text",
            "content": (
                f"\n\n> 已在画布生成分析报告（{block_count} 个落块），"
                "图表与叙事段落已就位，可自由拖拽调整布局。"
            ),
        })
        await emit({"type": "done"})
        return {}
