"""PlannerAgent：动态任务规划——把用户任务分解为工具调用计划。

与旧版（固定 SQL→Chart 流水线）不同，新版 Planner 生成的计划是**动态的工具调用序列**：
根据任务复杂度自由组合 ToolRegistry 中的任意工具，带依赖关系，交给执行器按计划执行。
"""
import json
import logging
from typing import Any, AsyncIterator

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.agents.base_agent import BaseAgent, AgentResult
from app.services.agent_tools import ToolRegistry
from app.services.llm_client import LLMClient
from app.services.observability import get_observer, observe_llm_call
from app.services.ai_prompts import ORCHESTRATOR_SYSTEM

logger = logging.getLogger(__name__)


# ==================== Task 5：Planner 输出 schema ====================

class PlanStep(BaseModel):
    """Planner 输出中的单个执行步骤。"""

    model_config = ConfigDict(extra="allow")

    step_id: int
    goal: str
    tool: str = ""
    depends_on: list[int] = Field(default_factory=list)
    purpose: str = ""


class PlanOutput(BaseModel):
    """Planner 输出的完整计划结构。"""

    model_config = ConfigDict(extra="allow")

    task_summary: str
    steps: list[PlanStep]
    expected_output: str = "report"


# ==================== Task 10：工具白名单标记化 ====================

# 兜底白名单：优先从 ToolRegistry 动态获取，异常/空集合时使用此常量
_ORCHESTRATOR_TOOLS_FALLBACK = frozenset({
    "list_datasources", "query_datasource", "query_engine", "data_quality",
    "insight", "clean_suggest", "recommend_charts", "render_chart",
    "validate_chart", "polish_text",
})
_ORCHESTRATOR_TOOLS_CACHE: frozenset | None = None


def get_orchestrator_tools() -> set[str]:
    """动态获取编排器可用工具白名单（来自 ToolRegistry 的 orchestrator_safe 标记）。"""
    try:
        tools = ToolRegistry.orchestrator_safe_tools()
    except Exception as e:
        logger.warning("获取 orchestrator 工具白名单失败，使用兜底常量: %s", e)
        return set(_ORCHESTRATOR_TOOLS_FALLBACK)
    if not tools:
        logger.warning("orchestrator 工具白名单为空，使用兜底常量")
        return set(_ORCHESTRATOR_TOOLS_FALLBACK)
    return set(tools)


def _get_cached_orchestrator_tools() -> frozenset:
    """进程内缓存的白名单 frozenset；缓存失效（None）后重新从 registry 派生。"""
    global _ORCHESTRATOR_TOOLS_CACHE
    if _ORCHESTRATOR_TOOLS_CACHE is None:
        _ORCHESTRATOR_TOOLS_CACHE = frozenset(get_orchestrator_tools())
    return _ORCHESTRATOR_TOOLS_CACHE


# ==================== Task 7：动态步骤上限 ====================

# 兼容常量；实际执行时使用 _estimate_max_steps() 按任务复杂度动态估算
MAX_STEPS = 8

_INTENT_KEYWORDS = (
    "趋势", "占比", "分布", "对比", "TOP", "排名", "归因",
    "分析", "报告", "报表", "汇总", "综合", "同比", "环比",
)
_MIN_PLAN_STEPS = 5
_MAX_PLAN_STEPS = 12


def _estimate_max_steps(user_msg, datasource_count=0) -> int:
    """根据任务复杂度与数据源数量估算最大步骤数，clamp 到 [_MIN_PLAN_STEPS, _MAX_PLAN_STEPS]。"""
    msg_lower = user_msg.lower()
    matched = sum(1 for kw in _INTENT_KEYWORDS if kw in msg_lower)
    intent_bonus = min(matched, 4)
    # 多数据源加成：1 个数据源无加成，2 个起加成，最多 +3
    datasource_bonus = 0 if datasource_count <= 1 else min(datasource_count, 3)
    raw = _MIN_PLAN_STEPS + intent_bonus + datasource_bonus
    return max(_MIN_PLAN_STEPS, min(_MAX_PLAN_STEPS, raw))


# 字段关键词 → 表描述关键词映射（用于自动推断表描述）
_FIELD_KEYWORD_MAP = {
    "order": "订单", "sale": "销售", "amount": "金额", "price": "价格",
    "revenue": "收入", "profit": "利润", "cost": "成本",
    "customer": "客户", "user": "用户", "member": "会员",
    "product": "商品", "item": "商品", "sku": "库存",
    "employee": "员工", "staff": "员工",
    "inventory": "库存", "stock": "库存",
    "payment": "支付", "transaction": "交易",
    "shipping": "物流", "delivery": "配送",
    "date": "时间", "time": "时间", "created": "创建时间",
    "region": "地区", "city": "城市", "country": "国家",
    "category": "分类", "type": "类型", "status": "状态",
}


def _infer_table_description(ds: dict) -> str:
    """根据数据源名称和字段信息自动推断表描述。"""
    name = ds.get("name", "")
    fields = ds.get("fields", [])
    field_names = [f.get("name", "").lower() for f in (fields if isinstance(fields, list) else [])]
    all_text = (name + " " + " ".join(field_names)).lower()

    keywords_found = []
    for keyword, desc in _FIELD_KEYWORD_MAP.items():
        if keyword in all_text and desc not in keywords_found:
            keywords_found.append(desc)

    if keywords_found:
        return f"包含{'、'.join(keywords_found[:4])}等信息的数据表"
    return "数据表"


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
                        response = await self.llm.complete(plan_prompt, temperature=0.2, max_tokens=3000)
                        llm_span.update(output=response[:500])

                    # 空响应重试：thinking 模式偶尔返回空 content，重试一次
                    if not response.strip():
                        self.log_warning("Planner 返回空响应，重试一次")
                        response = await self.llm.complete(plan_prompt, temperature=0.3, max_tokens=3000)

                    plan = await self._validate_and_retry(plan_prompt, response)

                    # 动态步骤上限：按任务复杂度截断
                    max_steps = _estimate_max_steps(user_msg, len(available_datasources))
                    steps = plan.get("steps", [])
                    if len(steps) > max_steps:
                        self.log_warning("计划步骤超过动态上限，截断", steps=len(steps), max_steps=max_steps)
                        plan["steps"] = steps[:max_steps]

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
                ds_name = ds.get("name", "")
                ds_desc = ds.get("description") or _infer_table_description(ds)
                fields = ds.get("fields", [])
                field_parts = []
                for f in (fields[:15] if isinstance(fields, list) else []):
                    name = f.get("name", "?")
                    dtype = f.get("data_type", "")
                    samples = f.get("sample", [])
                    sample_str = f" 示例值={samples}" if samples else ""
                    field_parts.append(f"{name}({dtype}{sample_str})" if dtype else name)
                field_str = ", ".join(field_parts)
                lines.append(f"- id={ds.get('id')} name={ds_name} 描述={ds_desc} type={ds.get('type')} 字段: {field_str}")
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

    # ============ Task 5：解析 + schema 校验 + 重试 ============

    def _extract_plan_json(self, response: str) -> dict:
        """从 LLM 响应中提取计划 JSON 对象。解析失败抛 JSONDecodeError / ValueError。"""
        json_str = response.strip()

        # 1. 尝试从代码块中提取 JSON
        if "```json" in response:
            json_str = response.split("```json")[1].split("```")[0]
        elif "```" in response:
            parts = response.split("```")
            for part in parts[1::2]:  # 奇数索引是代码块内容
                part = part.strip()
                if part.startswith("{") or part.startswith("["):
                    json_str = part
                    break

        # 2. 如果直接解析失败，尝试找第一个 { 到最后一个 } 之间的内容
        if not json_str.strip().startswith("{"):
            first_brace = json_str.find("{")
            last_brace = json_str.rfind("}")
            if first_brace != -1 and last_brace > first_brace:
                json_str = json_str[first_brace:last_brace + 1]

        plan = json.loads(json_str)  # 可能抛 JSONDecodeError
        if not isinstance(plan, dict):
            raise ValueError("计划不是 JSON 对象")
        raw_steps = plan.get("steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError("steps 必须是数组")
        return plan

    def _normalize_plan(self, plan: dict) -> dict:
        """Schema 校验 + 工具白名单过滤 + 字段规范化。返回规范化后的计划 dict。"""
        structured = PlanOutput.model_validate(plan)  # 抛 ValidationError 时由调用方处理

        safe_tools = _get_cached_orchestrator_tools()
        valid_steps: list[dict] = []
        seen_ids: set[int] = set()
        for s in structured.steps:
            sid = s.step_id
            tool = s.tool or ""
            if sid in seen_ids:
                continue
            if tool and tool not in safe_tools:
                self.log_warning("计划中包含未注册工具，已跳过", tool=tool)
                continue
            seen_ids.add(sid)
            # 骨架步骤：只有 goal / tool(建议) / depends_on，无 args（参数由执行 Agent 实时生成）
            valid_steps.append({
                "step_id": sid,
                "goal": s.goal[:200],
                "tool": tool,
                "depends_on": [d for d in s.depends_on if isinstance(d, int)],
                "purpose": s.purpose[:120],
            })

        plan["steps"] = valid_steps
        plan["task_summary"] = str(structured.task_summary)[:120]
        plan["expected_output"] = str(structured.expected_output or "report")[:20]
        return plan

    def _build_fallback_plan(self) -> dict:
        """降级计划：单步骨架计划（先列出数据源）。"""
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

    async def _validate_and_retry(self, plan_prompt, response) -> dict:
        """解析 + schema 校验；校验失败时把具体错误回喂 LLM 重试一次，仍失败走 fallback。"""
        # 1. JSON 解析失败 → 不触发 schema 重试，直接 fallback（原有行为不变）
        try:
            plan = self._extract_plan_json(response)
        except (json.JSONDecodeError, ValueError) as e:
            self.log_error("解析计划 JSON 失败", error=str(e), response=response[:300])
            return self._build_fallback_plan()

        # 2. schema 校验通过 → 直接返回规范化计划
        try:
            return self._normalize_plan(plan)
        except ValidationError as e:
            # 3. schema 校验失败：把具体错误回喂 LLM 重试一次
            feedback = (
                f"上一步生成的计划 schema 校验失败，请修正后重新生成。校验错误：{e}"
            )
            retry_messages = list(plan_prompt) + [{"role": "user", "content": feedback}]
            retry_response = await self.llm.complete(retry_messages, temperature=0.2, max_tokens=3000)
            try:
                retry_plan = self._extract_plan_json(retry_response)
                return self._normalize_plan(retry_plan)
            except (json.JSONDecodeError, ValueError, ValidationError) as e2:
                self.log_error("重试后计划仍校验失败，使用 fallback", error=str(e2))
                return self._build_fallback_plan()