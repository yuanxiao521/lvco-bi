"""PlannerAgent：动态任务规划——把用户任务分解为工具调用计划。

与旧版（固定 SQL→Chart 流水线）不同，新版 Planner 生成的计划是**动态的工具调用序列**：
根据任务复杂度自由组合 ToolRegistry 中的任意工具，带依赖关系，交给执行器按计划执行。
"""
import json
import logging
from typing import Any, AsyncIterator

from app.services.agents.base_agent import BaseAgent, AgentResult
from app.services.llm_client import LLMClient
from app.services.observability import get_observer, observe_llm_call
from app.services.ai_prompts import ORCHESTRATOR_SYSTEM

logger = logging.getLogger(__name__)

# 编排器可用的工具白名单（与 ToolRegistry 注册一致，供规划校验）
_ORCHESTRATOR_TOOLS = frozenset({
    "list_datasources", "query_datasource", "query_engine", "data_quality",
    "insight", "clean_suggest", "recommend_charts", "render_chart",
    "validate_chart", "polish_text",
})

MAX_STEPS = 8


class PlannerAgent(BaseAgent):
    """任务规划 Agent：分析用户意图，动态生成工具调用计划"""

    def __init__(self, llm: LLMClient):
        super().__init__("planner")
        self.llm = llm

    async def execute(self, **kwargs) -> AgentResult:
        """分析用户输入，生成工具调用计划。"""
        user_msg = kwargs.get("user_msg", "")
        history = kwargs.get("history", [])
        available_datasources = kwargs.get("available_datasources", [])

        self.log_info("开始任务规划",
            user_msg_length=len(user_msg),
            history_count=len(history),
            datasource_count=len(available_datasources),
        )

        observer = get_observer()
        with self.track_execution("plan_generation"):
            with observer.trace("planner_agent_execute") as trace:
                try:
                    plan_prompt = self._build_plan_prompt(user_msg, history, available_datasources)
                    with observe_llm_call(trace, "planner_llm_call", messages=plan_prompt, model="planner") as llm_span:
                        response = await self.llm.complete(plan_prompt, temperature=0.2, max_tokens=1500)
                        llm_span.update(output=response[:500])

                    plan = self._parse_plan(response)
                    steps_count = len(plan.get("steps", []))
                    self.log_info("任务规划完成",
                        steps_count=steps_count,
                        task_summary=plan.get("task_summary", "")[:80],
                        expected_output=plan.get("expected_output"),
                    )
                    return AgentResult(success=True, data=plan, metadata={"raw_response": response})

                except Exception as e:
                    self.log_exception("任务规划失败", error=str(e), user_msg_preview=user_msg[:100])
                    return AgentResult(success=False, error=str(e))

    async def stream_execute(self, **kwargs) -> AsyncIterator[dict]:
        """流式规划（兼容基类接口）。"""
        result = await self.execute(**kwargs)
        if result.success:
            yield {"type": "planner_plan", "plan": result.data}
        else:
            yield {"type": "planner_error", "message": f"规划失败: {result.error}"}

    def _build_plan_prompt(self, user_msg, history, available_datasources) -> list[dict[str, str]]:
        """构建规划提示词：系统约束 + 数据源信息 + 用户任务。"""
        ds_info = ""
        if available_datasources:
            lines = []
            for ds in available_datasources[:20]:
                fields = ds.get("fields", [])
                field_str = ", ".join(f.get("name", "?") for f in fields[:10]) if isinstance(fields, list) else ""
                lines.append(f"- id={ds.get('id')} name={ds.get('name')} type={ds.get('type')} 字段: {field_str}")
            ds_info = "\n".join(lines)
        else:
            ds_info = "（无数据源信息，第 1 步应规划 list_datasources）"

        context = f"用户任务：{user_msg}\n\n可用数据源：\n{ds_info}"
        if history:
            context += f"\n\n对话历史（最近）：\n{json.dumps(history[-5:], ensure_ascii=False)}"

        messages = [
            {"role": "system", "content": ORCHESTRATOR_SYSTEM},
            {"role": "user", "content": context + "\n\n请生成执行计划（严格 JSON）。"},
        ]
        return messages

    def _parse_plan(self, response: str) -> dict[str, Any]:
        """解析并校验 LLM 返回的计划 JSON。"""
        try:
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]
            else:
                json_str = response

            plan = json.loads(json_str)

            # 校验结构
            if not isinstance(plan, dict):
                raise ValueError("计划不是 JSON 对象")
            raw_steps = plan.get("steps", [])
            if not isinstance(raw_steps, list):
                raise ValueError("steps 必须是数组")

            # 规范化 + 校验步骤
            valid_steps: list[dict] = []
            seen_ids: set[int] = set()
            for s in raw_steps:
                if not isinstance(s, dict):
                    continue
                sid = s.get("step_id")
                tool = s.get("tool")
                if not isinstance(sid, int) or sid in seen_ids:
                    continue
                if tool and tool not in _ORCHESTRATOR_TOOLS:
                    self.log_warning("计划中包含未注册工具，已跳过", tool=tool)
                    continue
                seen_ids.add(sid)
                deps = s.get("depends_on") or []
                if not isinstance(deps, list):
                    deps = []
                # 骨架步骤：只有 goal / tool(建议) / depends_on，无 args（参数由执行 Agent 实时生成）
                valid_steps.append({
                    "step_id": sid,
                    "goal": str(s.get("goal") or s.get("purpose") or "")[:200],
                    "tool": tool or "",
                    "depends_on": [d for d in deps if isinstance(d, int)],
                    "purpose": str(s.get("purpose", ""))[:120],
                })

            # 截断上限
            plan["steps"] = valid_steps[:MAX_STEPS]
            plan["task_summary"] = str(plan.get("task_summary", ""))[:120]
            plan["expected_output"] = str(plan.get("expected_output", "report"))[:20]
            return plan

        except (json.JSONDecodeError, ValueError) as e:
            self.log_error("解析计划 JSON 失败", error=str(e), response=response[:300])
            # 降级：单步骨架计划（先列出数据源）
            return {
                "task_summary": "fallback",
                "steps": [{
                    "step_id": 1,
                    "goal": "列出可用数据源，确定要分析的数据源",
                    "tool": "list_datasources",
                    "depends_on": [],
                    "purpose": "列出可用数据源",
                }],
                "expected_output": "text",
            }

