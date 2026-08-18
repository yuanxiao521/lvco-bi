"""AgentOrchestrator：多 Agent 编排器"""
import json
import logging
import time
from typing import Any, AsyncIterator

from app.services.agents.base_agent import AgentResult
from app.services.agents.planner_agent import PlannerAgent
from app.services.agents.sql_agent import SQLAgent
from app.services.agents.chart_agent import ChartAgent
from app.services.llm_client import LLMClient
from app.services.observability import get_observer

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """多 Agent 编排器：协调 Planner、SQL、Chart Agent 完成复杂任务"""
    
    def __init__(self, llm: LLMClient, db_session):
        self.llm = llm
        self.db_session = db_session
        
        # 初始化三个专业 Agent
        self.planner = PlannerAgent(llm)
        self.sql_agent = SQLAgent(llm, db_session)
        self.chart_agent = ChartAgent(llm)
        
        logger.info("AgentOrchestrator 初始化完成")
    
    async def execute_task(self, user_msg: str, history: list[dict], **kwargs) -> AsyncIterator[dict]:
        """执行用户任务：规划 → 查询 → 可视化"""
        user_id = kwargs.get("user_id", "")
        start_time = time.time()
        
        logger.info(f"[orchestrator] 开始执行任务 user_id={user_id} msg_length={len(user_msg)} history_count={len(history)}")
        
        observer = get_observer()
        with observer.trace("orchestrator_execute") as trace:
            try:
                # 步骤 1: 任务规划
                logger.info(f"[orchestrator] 步骤 1: 开始任务规划")
                yield {"type": "status", "message": "正在分析您的需求..."}
                
                plan_start = time.time()
                plan_result = await self.planner.execute(
                    user_msg=user_msg,
                    history=history,
                    available_datasources=kwargs.get("available_datasources", [])
                )
                plan_elapsed = int((time.time() - plan_start) * 1000)
                
                if not plan_result.success:
                    logger.error(f"[orchestrator] 任务规划失败: {plan_result.error} elapsed_ms={plan_elapsed}")
                    yield {"type": "error", "message": f"任务规划失败: {plan_result.error}"}
                    return
                
                plan = plan_result.data
                steps_count = len(plan.get("steps", []))
                logger.info(f"[orchestrator] 规划完成 steps={steps_count} elapsed_ms={plan_elapsed}")
                
                yield {
                    "type": "plan",
                    "plan": plan
                }
                
                # 步骤 2: 根据规划执行查询
                query_result = None
                query_steps = [s for s in plan.get("steps", []) if s.get("action") in ["query_data", "query_datasource"]]
                logger.info(f"[orchestrator] 步骤 2: 开始执行查询 query_steps={len(query_steps)}")
                
                for idx, step in enumerate(query_steps, 1):
                    action = step.get("action", "")
                    logger.info(f"[orchestrator] 执行查询步骤 {idx}/{len(query_steps)} action={action}")
                    yield {"type": "status", "message": f"正在查询数据 ({idx}/{len(query_steps)})..."}
                    
                    query_start = time.time()
                    query_result = await self.sql_agent.execute(
                        user_msg=user_msg,
                        datasource_info=step.get("datasource_info", {}),
                        query_context=step.get("parameters", {})
                    )
                    query_elapsed = int((time.time() - query_start) * 1000)
                    
                    if not query_result.success:
                        logger.error(f"[orchestrator] SQL 查询失败: {query_result.error} elapsed_ms={query_elapsed}")
                        yield {"type": "error", "message": f"数据查询失败: {query_result.error}"}
                        return
                    
                    row_count = len(query_result.data.get("result", []))
                    logger.info(f"[orchestrator] 查询步骤 {idx} 完成 row_count={row_count} elapsed_ms={query_elapsed}")
                    
                    yield {
                        "type": "sql_result",
                        "sql": query_result.data.get("sql"),
                        "data": query_result.data.get("result"),
                        "row_count": row_count
                    }
                
                # 步骤 3: 生成图表（如果有查询结果）
                if query_result and query_result.success:
                    logger.info(f"[orchestrator] 步骤 3: 开始生成图表")
                    yield {"type": "status", "message": "正在生成图表..."}
                    
                    chart_start = time.time()
                    chart_result = await self.chart_agent.execute(
                        query_result=query_result.data,
                        user_intent=user_msg,
                        chart_preferences=kwargs.get("chart_preferences", {})
                    )
                    chart_elapsed = int((time.time() - chart_start) * 1000)
                    
                    if chart_result.success:
                        chart_type = chart_result.data.get("chart_type")
                        logger.info(f"[orchestrator] 图表生成成功 type={chart_type} elapsed_ms={chart_elapsed}")
                        yield {
                            "type": "chart",
                            "chart_type": chart_type,
                            "option": chart_result.data.get("option")
                        }
                    else:
                        logger.warning(f"[orchestrator] 图表生成失败: {chart_result.error} elapsed_ms={chart_elapsed}")
                        yield {"type": "warning", "message": f"图表生成失败: {chart_result.error}"}
                else:
                    logger.info(f"[orchestrator] 步骤 3: 跳过图表生成（无查询结果）")
                
                # 步骤 4: 生成分析报告
                logger.info(f"[orchestrator] 步骤 4: 开始生成分析报告")
                yield {"type": "status", "message": "正在生成分析报告..."}
                
                report_start = time.time()
                report = await self._generate_report(
                    user_msg=user_msg,
                    plan=plan,
                    query_result=query_result.data if query_result else None
                )
                report_elapsed = int((time.time() - report_start) * 1000)
                
                logger.info(f"[orchestrator] 报告生成完成 report_length={len(report)} elapsed_ms={report_elapsed}")
                
                yield {
                    "type": "text",
                    "content": report
                }
                
                yield {"type": "done"}
                
                total_elapsed = int((time.time() - start_time) * 1000)
                logger.info(f"[orchestrator] 任务执行完成 user_id={user_id} total_elapsed_ms={total_elapsed}")
                
            except Exception as e:
                total_elapsed = int((time.time() - start_time) * 1000)
                logger.error(f"[orchestrator] 任务执行异常: {e} elapsed_ms={total_elapsed}", exc_info=True)
                yield {"type": "error", "message": f"执行异常: {str(e)}"}
    
    async def _generate_report(
        self,
        user_msg: str,
        plan: dict,
        query_result: dict | None
    ) -> str:
        """生成分析报告"""
        try:
            report_prompt = self._build_report_prompt(user_msg, plan, query_result)
            
            response = await self.llm.complete(report_prompt)
            
            return response
            
        except Exception as e:
            logger.error(f"生成报告失败: {e}")
            return "报告生成失败，请稍后重试。"
    
    def _build_report_prompt(
        self,
        user_msg: str,
        plan: dict,
        query_result: dict | None
    ) -> list[dict[str, str]]:
        """构建报告生成提示词"""
        system_prompt = """你是一个数据分析专家。根据用户的查询需求、执行计划和查询结果，生成一份清晰的分析报告。

报告要求：
1. 用简洁的语言总结分析结果
2. 突出关键数据和发现
3. 提供有价值的洞察和建议
4. 使用 Markdown 格式，结构清晰

用户问题: {user_msg}

执行计划: {plan}

查询结果: {result}

请生成分析报告。
"""
        
        messages = [
            {"role": "system", "content": system_prompt.format(
                user_msg=user_msg,
                plan=json.dumps(plan, ensure_ascii=False),
                result=json.dumps(query_result, ensure_ascii=False) if query_result else "无查询结果"
            )}
        ]
        
        messages.append({"role": "user", "content": "请生成分析报告"})
        
        return messages
