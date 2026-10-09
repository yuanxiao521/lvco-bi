"""PlannerAgent：任务规划与分解"""
import json
import logging
from typing import Any, AsyncIterator

from app.services.agents.base_agent import BaseAgent, AgentResult
from app.services.llm_client import LLMClient
from app.services.observability import get_observer, observe_llm_call

logger = logging.getLogger(__name__)


class PlannerAgent(BaseAgent):
    """任务规划 Agent：分析用户意图，分解为可执行的子任务"""
    
    def __init__(self, llm: LLMClient):
        super().__init__("planner")
        self.llm = llm
    
    async def execute(self, **kwargs) -> AgentResult:
        """分析用户输入，生成任务执行计划"""
        user_msg = kwargs.get("user_msg", "")
        history = kwargs.get("history", [])
        available_datasources = kwargs.get("available_datasources", [])
        
        self.log_info("开始任务规划",
            user_msg_length=len(user_msg),
            history_count=len(history),
            datasource_count=len(available_datasources)
        )
        
        observer = get_observer()
        with self.track_execution("plan_generation"):
            with observer.trace("planner_agent_execute") as trace:
                try:
                    # 构建规划提示词
                    plan_prompt = self._build_plan_prompt(
                        user_msg, history, available_datasources
                    )
                    
                    self.log_debug("提示词构建完成",
                        message_count=len(plan_prompt),
                        total_length=sum(len(m.get("content", "")) for m in plan_prompt)
                    )
                    
                    with observe_llm_call(
                        trace, "planner_llm_call",
                        messages=plan_prompt,
                        model="planner"
                    ) as llm_span:
                        self.log_debug("开始调用 LLM 生成规划")
                        response = await self.llm.complete(plan_prompt)
                        llm_span.update(output=response[:500])
                        self.log_debug("LLM 调用完成", response_length=len(response))
                    
                    # 解析规划结果
                    plan = self._parse_plan(response)
                    
                    self.log_info("任务规划完成",
                        steps_count=len(plan.get("steps", [])),
                        primary_intent=plan.get("primary_intent"),
                        expected_output=plan.get("expected_output")
                    )
                    
                    return AgentResult(
                        success=True,
                        data=plan,
                        metadata={"raw_response": response}
                    )
                    
                except Exception as e:
                    self.log_exception("任务规划失败", error=str(e), user_msg_preview=user_msg[:100])
                    return AgentResult(
                        success=False,
                        error=str(e),
                        metadata={"user_msg": user_msg}
                    )
    
    async def stream_execute(self, **kwargs) -> AsyncIterator[dict]:
        """流式输出规划过程"""
        user_msg = kwargs.get("user_msg", "")
        history = kwargs.get("history", [])
        
        self.log_info("开始流式任务规划")
        
        # 输出规划开始事件
        yield {
            "type": "planner_start",
            "message": "正在分析您的需求..."
        }
        
        # 执行规划
        result = await self.execute(**kwargs)
        
        if result.success:
            yield {
                "type": "planner_plan",
                "plan": result.data
            }
        else:
            yield {
                "type": "planner_error",
                "message": f"规划失败: {result.error}"
            }
    
    def _build_plan_prompt(
        self,
        user_msg: str,
        history: list[dict],
        available_datasources: list[dict]
    ) -> list[dict[str, str]]:
        """构建规划提示词"""
        system_prompt = """你是一个数据分析任务规划专家。你的工作是：
1. 理解用户的分析需求
2. 将复杂需求分解为可执行的步骤
3. 确定每个步骤需要的工具和数据来源

可用的数据源：
{datasources}

请分析用户需求，输出 JSON 格式的执行计划：
{{
  "primary_intent": "用户的主要意图（如：数据查询、图表生成、数据分析等）",
  "steps": [
    {{
      "step_id": 1,
      "action": "具体动作（如：list_datasources, query_data, generate_chart）",
      "target": "目标对象",
      "parameters": {{}},
      "depends_on": []
    }}
  ],
  "expected_output": "期望的输出类型（text, chart, report）"
}}
"""
        
        # 格式化数据源信息
        datasource_info = "\n".join([
            f"- {ds.get('name', 'Unknown')} (ID: {ds.get('id', 'N/A')})"
            for ds in available_datasources
        ]) if available_datasources else "暂无可用数据源"
        
        messages = [
            {"role": "system", "content": system_prompt.format(datasources=datasource_info)}
        ]
        
        # 添加历史上下文
        if history:
            messages.append({
                "role": "system",
                "content": f"对话历史：\n{json.dumps(history[-5:], ensure_ascii=False)}"
            })
        
        messages.append({"role": "user", "content": user_msg})
        
        return messages
    
    def _parse_plan(self, response: str) -> dict[str, Any]:
        """解析 LLM 返回的规划"""
        try:
            # 尝试提取 JSON
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]
            else:
                json_str = response
            
            plan = json.loads(json_str)
            
            # 确保必要字段存在
            if "steps" not in plan:
                plan["steps"] = []
            if "primary_intent" not in plan:
                plan["primary_intent"] = "unknown"
            
            return plan
            
        except json.JSONDecodeError as e:
            self.log_error("解析规划 JSON 失败", error=str(e), response=response[:200])
            # 返回默认规划
            return {
                "primary_intent": "fallback",
                "steps": [{
                    "step_id": 1,
                    "action": "list_datasources",
                    "target": "all",
                    "parameters": {},
                    "depends_on": []
                }],
                "expected_output": "text"
            }
