"""SQLAgent：数据查询与 SQL 执行"""
import json
import logging
from typing import Any, AsyncIterator

from app.services.agents.base_agent import BaseAgent, AgentResult
from app.services.llm_client import LLMClient
from app.services.observability import get_observer, observe_llm_call, observe_tool_call
from app.services.sql_guard import sql_guard

logger = logging.getLogger(__name__)


class SQLGuardBlockedError(Exception):
    """SQL 被三层防护拦截时抛出。"""

    def __init__(self, reason: str, blocked_sql: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.blocked_sql = blocked_sql


class SQLAgent(BaseAgent):
    """SQL 查询 Agent：负责生成和执行 SQL 查询"""
    
    def __init__(self, llm: LLMClient, db_session):
        super().__init__("sql")
        self.llm = llm
        self.db_session = db_session
    
    async def execute(self, **kwargs) -> AgentResult:
        """执行 SQL 查询任务"""
        user_msg = kwargs.get("user_msg", "")
        datasource_info = kwargs.get("datasource_info", {})
        query_context = kwargs.get("query_context", {})
        
        self.log_info("开始 SQL 查询",
            user_msg_length=len(user_msg),
            datasource_id=datasource_info.get("id"),
            datasource_name=datasource_info.get("name")
        )
        
        observer = get_observer()
        with self.track_execution("sql_query"):
            with observer.trace("sql_agent_execute") as trace:
                try:
                    # 安全检查
                    self.log_debug("执行 SQL 安全检查")
                    guard_result = sql_guard.full_check(user_msg)
                    if not guard_result.allowed:
                        self.log_warning("SQL 安全检查失败", 
                            reason=guard_result.reason,
                            user_msg_preview=user_msg[:100]
                        )
                        return AgentResult(
                            success=False,
                            error=f"安全检查失败: {guard_result.reason}"
                        )
                    self.log_debug("SQL 安全检查通过")
                    
                    # 生成 SQL
                    self.log_debug("开始构建 SQL 提示词")
                    sql_prompt = self._build_sql_prompt(
                        user_msg, datasource_info, query_context
                    )
                    self.log_debug("SQL 提示词构建完成",
                        message_count=len(sql_prompt),
                        has_context=bool(query_context)
                    )
                    
                    with observe_llm_call(
                        trace, "sql_generation",
                        messages=sql_prompt,
                        model="sql"
                    ) as llm_span:
                        self.log_debug("开始调用 LLM 生成 SQL")
                        response = await self.llm.complete(sql_prompt)
                        llm_span.update(output=response[:500])
                        self.log_debug("LLM SQL 生成完成", response_length=len(response))
                    
                    # 解析 SQL
                    sql_query = self._extract_sql(response)

                    if not sql_query:
                        self.log_warning("未能从响应中提取 SQL",
                            response_preview=response[:200]
                        )
                        return AgentResult(
                            success=False,
                            error="未能从响应中提取 SQL 查询"
                        )

                    self.log_info("SQL 生成成功", sql_length=len(sql_query))
                    self.log_debug("生成的 SQL", sql_preview=sql_query[:300])

                    # P0 安全修复：把 LLM 生成的 SQL 再喂给 L3 校验
                    # L3 内部会自动 LIMIT 注入（LIMIT 不存在时），并返回 sanitized_sql
                    sql_l3 = sql_guard.full_check(user_msg, sql_query)
                    if not sql_l3.allowed:
                        self.log_warning(
                            "LLM 生成的 SQL 被 L3 拦截",
                            layer=sql_l3.layer,
                            reason=sql_l3.reason,
                            sql_preview=sql_query[:200],
                        )
                        return AgentResult(
                            success=False,
                            error=f"生成的 SQL 未通过安全检查: {sql_l3.reason}",
                            data={"blocked_sql": sql_query[:200], "layer": sql_l3.layer},
                        )
                    safe_sql = sql_l3.sanitized_sql or sql_query

                    # 执行 SQL
                    self.log_debug("开始执行 SQL 查询")
                    with observe_tool_call(
                        trace, "execute_sql",
                        args={"sql": safe_sql}
                    ) as tool_span:
                        result = await self._execute_sql(safe_sql)
                        tool_span.update(output=str(result)[:500])

                        row_count = len(result) if isinstance(result, list) else 0
                        self.log_info("SQL 查询执行完成",
                            row_count=row_count,
                            has_data=row_count > 0
                        )

                    return AgentResult(
                        success=True,
                        data={
                            "sql": safe_sql,
                            "result": result,
                            "datasource": datasource_info
                        },
                        metadata={"response": response}
                    )
                    
                except Exception as e:
                    self.log_exception("SQL 查询失败",
                        error=str(e),
                        datasource_id=datasource_info.get("id"),
                        user_msg_preview=user_msg[:100]
                    )
                    return AgentResult(
                        success=False,
                        error=str(e)
                    )
    
    async def stream_execute(self, **kwargs) -> AsyncIterator[dict]:
        """流式执行 SQL 查询"""
        user_msg = kwargs.get("user_msg", "")
        
        self.log_info("开始流式 SQL 查询")
        
        yield {
            "type": "sql_start",
            "message": "正在生成 SQL 查询..."
        }
        
        result = await self.execute(**kwargs)
        
        if result.success:
            yield {
                "type": "sql_result",
                "sql": result.data.get("sql"),
                "data": result.data.get("result"),
                "row_count": len(result.data.get("result", []))
            }
        else:
            yield {
                "type": "sql_error",
                "message": f"查询失败: {result.error}"
            }
    
    def _build_sql_prompt(
        self,
        user_msg: str,
        datasource_info: dict,
        query_context: dict
    ) -> list[dict[str, str]]:
        """构建 SQL 生成提示词"""
        system_prompt = """你是一个 SQL 查询专家。根据用户的自然语言描述和数据源信息，生成准确的 SQL 查询语句。

数据源信息：
- 表名: {table_name}
- 字段: {fields}
- 数据源类型: {source_type}

请生成 SQL 查询，只返回 SQL 语句本身，不要包含解释。使用 ```sql 代码块格式。
"""
        
        fields_info = "\n".join([
            f"  - {f.get('name')} ({f.get('type', 'unknown')})"
            for f in datasource_info.get("fields", [])
        ])
        
        messages = [
            {"role": "system", "content": system_prompt.format(
                table_name=datasource_info.get("table_ref", "unknown"),
                fields=fields_info,
                source_type=datasource_info.get("type", "unknown")
            )}
        ]
        
        # 添加上下文
        if query_context:
            messages.append({
                "role": "system",
                "content": f"查询上下文: {json.dumps(query_context, ensure_ascii=False)}"
            })
        
        messages.append({"role": "user", "content": user_msg})
        
        return messages
    
    def _extract_sql(self, response: str) -> str | None:
        """从 LLM 响应中提取 SQL"""
        # 尝试提取 ```sql ... ``` 代码块
        if "```sql" in response:
            sql = response.split("```sql")[1].split("```")[0].strip()
            return sql
        elif "```" in response:
            sql = response.split("```")[1].split("```")[0].strip()
            return sql
        return None
    
    async def _execute_sql(self, sql: str) -> list[dict]:
        """执行 SQL 查询（纵深防御：执行前再过一次 L3）"""
        from app.core.duckdb_client import duckdb_client

        re_guard = sql_guard.full_check("", sql)
        if not re_guard.allowed:
            self.log_error("SQL 被 L3 拦截", sql=sql[:200], reason=re_guard.reason)
            raise SQLGuardBlockedError(re_guard.reason or "L3 拦截")

        safe_sql = re_guard.sanitized_sql or sql

        try:
            result = await duckdb_client.fetchall(safe_sql)
            return result
        except Exception as e:
            self.log_error("SQL 执行失败", sql=safe_sql, error=str(e))
            raise
