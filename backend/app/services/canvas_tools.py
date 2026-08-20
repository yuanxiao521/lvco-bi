"""画布操作工具：让 Agent 能直接驱动分析画布（加图表/加文本/改块/删块/布局）。

这些工具本身**不写数据库**——它们只做"查询验证 + 产出 canvas_action"，
前端收到 canvas_action SSE 事件后在画布上实时落块并保存（前端是 blocks 的唯一写者）。
canvas_action 内嵌在工具返回 JSON 中，由 API 层提取后转发为独立 SSE 事件。
"""
import json
import logging
from uuid import UUID

from app.services.agent_tools import BaseTool
from app.schemas.query import ChartQueryConfig, MeasureConfig
from app.services.metric_service import MetricServiceError
from app.services.query_engine import QueryEngineError, execute_chart_query

logger = logging.getLogger("lvco.canvas_tools")

# 合法聚合方式（与 query_engine.ALLOWED_AGGREGATIONS 对齐，供 LLM schema 提示）
ALLOWED_AGGS = ["SUM", "COUNT", "AVG", "MAX", "MIN"]

# 合法图表类型（与前端 VALID_CHART_TYPES / ChartType 对齐）
CHART_TYPES = [
    "bar", "line", "pie", "donut", "area", "scatter", "kpi_card",
    "grouped_bar", "stacked_bar", "horizontal_bar",
    "funnel", "heatmap", "radar", "sankey",
]

# canvas_action 中携带的最大数据行数，避免 SSE 包过大
_CANVAS_MAX_ROWS = 50

# 画布工具名：供编排器（Planner）按入口注入为可规划工具。
# 仅画布接口会注入，普通 AI 对话不注入，避免规划到无画布可落的工具。
CANVAS_TOOL_NAMES = frozenset({
    "add_chart_block", "add_text_block",
    "update_chart_block", "remove_block", "arrange_layout",
})


def _chart_type_schema():
    return {"type": "string", "enum": CHART_TYPES}


def _measure_schema():
    return {
        "type": "array",
        "items": {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "metric_key": {"type": "string", "description": "引用指标中心已定义的命名指标 key，如 sales_amount"},
                        "field": {"type": "string", "description": "当指标是模板指标（formula 含 {{field}}）时，用于填充其占位字段的字段名"},
                    },
                    "required": ["metric_key"],
                },
                {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "度量字段名，必须与数据源字段完全一致"},
                        "agg": {"type": "string", "enum": ALLOWED_AGGS},
                    },
                    "required": ["field", "agg"],
                },
            ]
        },
    }


def _dimension_schema():
    return {"type": "array", "items": {"type": "string"}}


class AddChartBlockTool(BaseTool):
    """在画布上新增一个图表块，会先用真实数据验证查询可行性并取数。"""

    name = "add_chart_block"
    description = (
        "在分析画布上新增一个图表块。会在后端先用真实数据验证查询可行性并取回数据，"
        "成功后才能被前端渲染。用于批量搭建分析报告。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "图表标题，如：渠道获客对比"},
                        "chart_type": _chart_type_schema(),
                        "datasource_id": {"type": "string", "description": "数据源 UUID"},
                        "dimensions": _dimension_schema(),
                        "measures": _measure_schema(),
                    },
                    "required": ["title", "chart_type", "datasource_id", "dimensions", "measures"],
                },
            },
        }

    async def execute(self, title: str, chart_type: str, datasource_id: str,
                      dimensions: list[str], measures: list[dict],
                      user_id: str = "", db_session=None, **kwargs) -> str:
        """验证查询可行性并取数，返回 canvas_action。

        measures 支持两种形态：{field, agg}（普通度量）或 {metric_id/metric_key, [field], [agg]}
        （引用指标中心的命名指标）。指标度量会被解析为当前口径的表达式，前端据此随口径刷新。

        查询失败时返回 error（触发 LLM 自纠错）；成功时返回含 rows/columns 的 canvas_action。
        """
        if not dimensions or not measures:
            return json.dumps({"error": "add_chart_block 需要至少一个维度和一个度量"}, ensure_ascii=False)

        try:
            from app.services.metric_service import resolve_measures
            m_configs, display_measures = await resolve_measures(
                db_session, UUID(user_id) if user_id else None, measures, dimensions=[str(d) for d in dimensions],
            )
            if not m_configs:
                return json.dumps({"error": "度量字段无效，每个度量需包含 field+agg 或 metric_id"}, ensure_ascii=False)
            config = ChartQueryConfig(
                dimensions=[str(d) for d in dimensions],
                measures=m_configs,
                filters=[],
                chart_type=chart_type,
                datasource_id=datasource_id,
                limit=_CANVAS_MAX_ROWS,
            )
            result = await execute_chart_query(
                datasource_id=UUID(datasource_id),
                config=config,
                user_id=UUID(user_id),
                db=db_session,
            )
        except (QueryEngineError, ValueError) as e:
            logger.warning("[add_chart_block] query_failed error=%s", str(e))
            return json.dumps({"error": f"查询验证失败: {str(e)}"}, ensure_ascii=False)
        except MetricServiceError as e:
            logger.warning("[add_chart_block] metric_error error=%s", str(e))
            return json.dumps({"error": f"指标解析失败: {str(e)}"}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("[add_chart_block] unexpected error=%s", str(e))
            return json.dumps({"error": f"生成图表失败: {str(e)}"}, ensure_ascii=False)

        rows = list(result.rows)[:_CANVAS_MAX_ROWS]
        action = {
            "action": "add_chart_block",
            "block": {
                "title": title,
                "chartType": chart_type,
                "datasourceId": datasource_id,
                "queryConfig": {
                    "dimensions": [str(d) for d in dimensions],
                    "measures": display_measures,
                    "filters": [],
                    "limit": _CANVAS_MAX_ROWS,
                },
                "columns": list(result.columns),
                "rows": rows,
            },
        }
        logger.info("[add_chart_block] ok title=%s chart_type=%s rows=%d", title, chart_type, len(rows))
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False, default=str)


class AddTextBlockTool(BaseTool):
    """在画布上新增文本/标题块。"""

    name = "add_text_block"
    description = (
        "在分析画布上新增一个文本块，用于写报告标题(h1)、章节标题(h2)或叙事段落(text)。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_type": {"type": "string", "enum": ["h1", "h2", "text"], "description": "h1 报告大标题 / h2 章节标题 / text 叙事段落"},
                        "content": {"type": "string", "description": "文本内容"},
                    },
                    "required": ["block_type", "content"],
                },
            },
        }

    async def execute(self, block_type: str, content: str, user_id: str = "",
                      db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此插入文本块。"""
        if block_type not in ("h1", "h2", "text"):
            return json.dumps({"error": "block_type 必须是 h1/h2/text"}, ensure_ascii=False)
        if not content or not content.strip():
            return json.dumps({"error": "文本内容不能为空"}, ensure_ascii=False)
        action = {"action": "add_text_block", "block": {"blockType": block_type, "content": content.strip()}}
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class UpdateChartBlockTool(BaseTool):
    """修改画布上已存在的图表块（改标题/类型/维度/度量）。"""

    name = "update_chart_block"
    description = (
        "修改画布上已存在图表块的标题、图表类型、维度或度量。block_id 来自上下文中已有的画布块。"
    )

    _PATCH_FIELDS = ("title", "chart_type", "dimensions", "measures")

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string", "description": "要修改的图表块 ID"},
                        "title": {"type": "string", "description": "新标题（可选）"},
                        "chart_type": _chart_type_schema(),
                        "dimensions": _dimension_schema(),
                        "measures": _measure_schema(),
                    },
                    "required": ["block_id"],
                },
            },
        }

    async def execute(self, block_id: str, user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此合并且重查图表。"""
        patch: dict = {}
        if kwargs.get("title") is not None:
            patch["title"] = kwargs["title"]
        if kwargs.get("chart_type") is not None:
            patch["chartType"] = kwargs["chart_type"]
        if kwargs.get("dimensions") is not None:
            patch["dimensions"] = [str(d) for d in kwargs["dimensions"]]
        if kwargs.get("measures") is not None:
            patch["measures"] = []
            for m in kwargs["measures"]:
                if not isinstance(m, dict):
                    continue
                if m.get("metric_id") or m.get("metric_key") or m.get("metricKey"):
                    patch["measures"].append({
                        "metric_id": m.get("metric_id") or m.get("metricId"),
                        "metric_key": m.get("metric_key") or m.get("metric_key"),
                    })
                elif m.get("field"):
                    patch["measures"].append({"field": m["field"], "agg": m.get("agg", "SUM")})
        if not patch:
            return json.dumps({"error": "没有提供任何要修改的字段"}, ensure_ascii=False)
        action = {"action": "update_chart_block", "blockId": block_id, "patch": patch}
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class RemoveBlockTool(BaseTool):
    """删除画布上的一个文本或图表块。"""

    name = "remove_block"
    description = (
        "删除画布上的一个块。block_id 来自上下文中已有的画布块。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string", "description": "要删除的块 ID"},
                    },
                    "required": ["block_id"],
                },
            },
        }

    async def execute(self, block_id: str, user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action，前端据此删除块。"""
        action = {"action": "remove_block", "blockId": block_id}
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


class ArrangeLayoutTool(BaseTool):
    """自动重排画布布局（前端报告式布局已实现）。"""

    name = "arrange_layout"
    description = (
        "自动重排画布上所有块为报告式布局：标题/文本通栏、图表双列网格。"
        "当画布块位置混乱或用户要求整理排版时调用。"
    )

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "layout": {"type": "string", "enum": ["auto"], "description": "自动布局模式"},
                    },
                    "required": ["layout"],
                },
            },
        }

    async def execute(self, layout: str = "auto", user_id: str = "", db_session=None, **kwargs) -> str:
        """返回 canvas_action；前端按报告式布局全量重排。"""
        action = {"action": "arrange_layout", "layout": layout}
        return json.dumps({"ok": True, "canvas_action": action}, ensure_ascii=False)


# ==================== 在 canvas_tools 自身底部注册 ====================
# 不反向 import 到 agent_tools，避免 partially-initialized 循环依赖。
# 本模块顶部只 import BaseTool；这里在类全部定义完后注册。
from app.services.agent_tools import ToolRegistry  # noqa: E402

ToolRegistry.register(AddChartBlockTool())
ToolRegistry.register(AddTextBlockTool())
ToolRegistry.register(UpdateChartBlockTool())
ToolRegistry.register(RemoveBlockTool())
ToolRegistry.register(ArrangeLayoutTool())