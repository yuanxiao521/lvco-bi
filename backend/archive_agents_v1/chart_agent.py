"""ChartAgent：图表生成与可视化"""
import json
import logging
from typing import Any, AsyncIterator

from app.services.agents.base_agent import BaseAgent, AgentResult
from app.services.llm_client import LLMClient
from app.services.observability import get_observer, observe_llm_call

logger = logging.getLogger(__name__)


class ChartAgent(BaseAgent):
    """图表生成 Agent：负责根据数据生成 ECharts 配置"""
    
    def __init__(self, llm: LLMClient):
        super().__init__("chart")
        self.llm = llm
    
    async def execute(self, **kwargs) -> AgentResult:
        """生成图表配置"""
        query_result = kwargs.get("query_result", {})
        user_intent = kwargs.get("user_intent", "")
        chart_preferences = kwargs.get("chart_preferences", {})
        
        data_rows = len(query_result.get("result", []))
        self.log_info("开始图表生成",
            data_rows=data_rows,
            user_intent_length=len(user_intent),
            has_preferences=bool(chart_preferences)
        )
        
        observer = get_observer()
        with self.track_execution("chart_generation"):
            with observer.trace("chart_agent_execute") as trace:
                try:
                    # 构建图表生成提示词
                    self.log_debug("开始构建图表提示词")
                    chart_prompt = self._build_chart_prompt(
                        query_result, user_intent, chart_preferences
                    )
                    self.log_debug("图表提示词构建完成",
                        message_count=len(chart_prompt),
                        total_length=sum(len(m.get("content", "")) for m in chart_prompt)
                    )
                    
                    with observe_llm_call(
                        trace, "chart_generation",
                        messages=chart_prompt,
                        model="chart"
                    ) as llm_span:
                        self.log_debug("开始调用 LLM 生成图表配置")
                        response = await self.llm.complete(chart_prompt)
                        llm_span.update(output=response[:500])
                        self.log_debug("LLM 图表生成完成", response_length=len(response))
                    
                    # 解析 ECharts 配置
                    self.log_debug("开始解析图表配置")
                    echarts_option = self._parse_chart_config(response)
                    
                    if not echarts_option:
                        self.log_warning("图表配置解析失败",
                            response_preview=response[:200]
                        )
                        return AgentResult(
                            success=False,
                            error="未能解析图表配置"
                        )
                    
                    chart_type = echarts_option.get("chart_type", "bar")
                    
                    self.log_info("图表生成完成",
                        chart_type=chart_type,
                        has_title="title" in echarts_option,
                        has_legend="legend" in echarts_option,
                        has_tooltip="tooltip" in echarts_option
                    )
                    
                    return AgentResult(
                        success=True,
                        data={
                            "chart_type": chart_type,
                            "option": echarts_option
                        },
                        metadata={"raw_response": response}
                    )
                    
                except Exception as e:
                    self.log_exception("图表生成失败",
                        error=str(e),
                        user_intent_preview=user_intent[:100]
                    )
                    return AgentResult(
                        success=False,
                        error=str(e)
                    )
    
    async def stream_execute(self, **kwargs) -> AsyncIterator[dict]:
        """流式生成图表"""
        self.log_info("开始流式图表生成")
        
        yield {
            "type": "chart_start",
            "message": "正在生成图表..."
        }
        
        result = await self.execute(**kwargs)
        
        if result.success:
            yield {
                "type": "chart",
                "chart_type": result.data.get("chart_type"),
                "option": result.data.get("option")
            }
        else:
            yield {
                "type": "chart_error",
                "message": f"图表生成失败: {result.error}"
            }
    
    def _build_chart_prompt(
        self,
        query_result: dict,
        user_intent: str,
        chart_preferences: dict
    ) -> list[dict[str, str]]:
        """构建图表生成提示词"""
        system_prompt = """你是一个数据可视化专家。根据查询数据和用户意图，生成合适的 ECharts 图表配置。

数据概览：
- 数据行数: {row_count}
- 字段: {columns}
- 示例数据: {sample_data}

用户意图: {user_intent}

请生成 ECharts option 配置，输出 JSON 格式，包含：
- chart_type: 图表类型（bar/line/pie/area/scatter等）
- option: 完整的 ECharts option 对象

使用 ```json 代码块格式输出。
"""
        
        data = query_result.get("result", [])
        columns = list(data[0].keys()) if data else []
        sample_data = data[:3] if data else []
        
        messages = [
            {"role": "system", "content": system_prompt.format(
                row_count=len(data),
                columns=", ".join(columns),
                sample_data=json.dumps(sample_data, ensure_ascii=False),
                user_intent=user_intent
            )}
        ]
        
        # 添加图表偏好
        if chart_preferences:
            messages.append({
                "role": "system",
                "content": f"图表偏好: {json.dumps(chart_preferences, ensure_ascii=False)}"
            })
        
        messages.append({
            "role": "user",
            "content": "请根据以上数据生成合适的图表配置"
        })
        
        return messages
    
    def _parse_chart_config(self, response: str) -> dict | None:
        """解析图表配置"""
        try:
            # 提取 JSON
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]
            else:
                json_str = response
            
            config = json.loads(json_str)
            
            # 确保包含必要字段
            if "option" not in config:
                # 假设整个响应就是 option
                config = {"chart_type": "bar", "option": config}
            
            return config
            
        except json.JSONDecodeError as e:
            self.log_error("解析图表配置失败",
                error=str(e),
                response=response[:200]
            )
            return None
