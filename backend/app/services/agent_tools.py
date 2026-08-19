"""Agent 工具注册：list_datasources, query_datasource, render_chart."""
import json
import logging
import re
from abc import ABC, abstractmethod
from enum import Enum

from app.core.duckdb_client import duckdb_client
from app.services.sql_guard import sql_guard, GuardResult

log = logging.getLogger("lvco.agent_tools")


# ==================== 对话阶段状态机 ====================

class ConversationPhase(str, Enum):
    """对话阶段枚举：控制每个阶段可用的工具，防止跨阶段调用。

    枚举值：
        SELECTING：选数据源阶段，只暴露 list_datasources 工具
        ANALYZING：查数据阶段，暴露 query_datasource 和 list_datasources（查询失败时自纠错需要）
        GENERATING：生图表阶段，只暴露 render_chart 工具
        REPORTING：出报告阶段，无工具可用，纯文本输出
    """
    SELECTING = "selecting"      # 选数据源 → 只暴露 list_datasources
    ANALYZING = "analyzing"      # 查数据   → 只暴露 query_datasource
    GENERATING = "generating"    # 生图表   → 只暴露 render_chart
    REPORTING = "reporting"      # 出报告   → 无工具，纯文本


# 各阶段可用工具集合（按职责映射，提升 Agent 协作能力）
# SELECTING:  只暴露数据源浏览
# ANALYZING:  查数据(query/query_engine) + 数据质量 + 洞察 + 清洗建议
# GENERATING: 图表生成(render) + 图表自校验(validate) + 类型推荐(recommend)
# REPORTING:  报告润色
_PHASE_TOOLS: dict[ConversationPhase, set[str]] = {
    ConversationPhase.SELECTING: {"list_datasources"},
    ConversationPhase.ANALYZING: {
        "list_datasources",
        "query_datasource",
        "query_engine",
        "data_quality",
        "insight",
        "clean_suggest",
        "stats_analyzer",
    },
    ConversationPhase.GENERATING: {
        "render_chart",
        "validate_chart",
        "recommend_charts",
    },
    ConversationPhase.REPORTING: {"polish_text"},
}


def get_tools_for_phase(phase: ConversationPhase, all_schemas: list[dict]) -> list[dict]:
    """根据当前对话阶段过滤可用工具。

    根据传入的对话阶段（phase），从全部工具 schema 列表中筛选出当前阶段允许使用的工具。

    参数：
        phase: 当前对话阶段（ConversationPhase 枚举值）
        all_schemas: 全部工具 schema 列表，每项包含 function name 等信息

    返回：
        过滤后的工具 schema 列表；REPORTING 阶段仅保留 polish_text
    """
    allowed = _PHASE_TOOLS.get(phase, set())
    return [s for s in all_schemas if s["function"]["name"] in allowed]


# ==================== AI 推荐图表 ECharts option 构建 ====================
# 镜像前端 buildMultiMeasureOption（echartsUtils.ts），保证双Y轴/图例/水平条形等
# 在 AI 推荐链路和画布手动配置链路下行为一致。
_MULTI_MEASURE_COLORS = [
    '#2BB5A0', '#6C7BF2', '#F5A623', '#EF5B5B',
    '#4EADFF', '#A78BFA', '#F472B6', '#34D399',
]


_AXIS_LABEL_FORMATTER = "__lvco_fmt_y__"


def _safe_float(v, default=0.0):
    """安全地将值转换为浮点数，转换失败时返回默认值（默认 0.0）。

    参数：
        v: 待转换的值，可以是数字、字符串或 None
        default: 转换失败时返回的默认值，默认为 0.0

    返回：
        转换后的浮点数，或默认值
    """
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _build_multi_measure_option(
    chart_type: str,
    columns: list,
    rows: list,
    title: str,
    horizontal: bool = False,
    stacked: bool = False,
) -> dict:
    """Python 版 multi_measure ECharts option 构建器（双Y轴 + PowerBI 图例 + 水平条形）。

    该函数镜向前端 buildMultiMeasureOption（echartsUtils.ts），
    保证 AI 推荐链路和画布手动配置链路下行为一致。
    当数据有 2+ 个度量值时，自动分配左/右双Y轴以提升可读性。

    参数：
        chart_type: 图表基础类型（"bar" / "line" / "area"）
        columns: 数据列名列表，第一列为维度列，后续为度量列
        rows: 数据行二维数组
        title: 图表标题
        horizontal: 是否水平布局（水平条形图时 True）
        stacked: 是否堆叠（堆叠柱状图时 True）

    返回：
        ECharts option 配置字典；若 columns 或 rows 为空则返回空字典
    """
    if not columns or not rows:
        return {}

    dim_col = columns[0]
    measure_cols = list(columns[1:])
    n_measures = len(measure_cols)
    use_dual_axis = n_measures >= 2
    is_area = chart_type == "area"
    is_stacked = stacked and chart_type == "bar"
    show_legend = n_measures > 1

    # 维度数据
    dims = [str(r[0]) if r else "" for r in rows]

    # 提取度量数值
    measure_values = []
    for m_col in measure_cols:
        m_idx = columns.index(m_col)
        vals = [_safe_float(r[m_idx] if m_idx < len(r) else 0) for r in rows]
        measure_values.append(vals)

    # 每列最大值，用于右轴分配
    maxes = [max(v) if v else 0 for v in measure_values]
    global_max = max(maxes) if maxes else 0

    # 右轴分配：明显小于最大值的放到右轴
    axis_assignments: list[int] = []
    for m in maxes:
        axis_assignments.append(1 if (use_dual_axis and m > 0 and m < global_max) else 0)
    # 至少保证左轴有一个度量
    if use_dual_axis and 0 not in axis_assignments and axis_assignments:
        axis_assignments[0] = 0

    # 左轴 + 右轴
    yaxis_list = []
    if use_dual_axis:
        # 左轴
        yaxis_list.append({
            "type": "value",
            "name": measure_cols[0],
            "nameTextStyle": {"color": _MULTI_MEASURE_COLORS[0], "fontSize": 11},
            "position": "left",
            "axisLine": {"show": True, "lineStyle": {"color": _MULTI_MEASURE_COLORS[0]}},
            "axisLabel": {"formatter": _AXIS_LABEL_FORMATTER, "color": "#8B97A8", "fontSize": 11},
            "splitLine": {"lineStyle": {"color": "#EEF1F6", "type": "dashed"}},
        })
        # 右轴：找第一个被分到右轴的度量
        right_idx = next((i for i, a in enumerate(axis_assignments) if a == 1), 0)
        right_color = _MULTI_MEASURE_COLORS[right_idx % len(_MULTI_MEASURE_COLORS)]
        right_name = measure_cols[right_idx] if right_idx < len(measure_cols) else ""
        yaxis_list.append({
            "type": "value",
            "name": right_name,
            "nameTextStyle": {"color": right_color, "fontSize": 11},
            "position": "right",
            "axisLine": {"show": True, "lineStyle": {"color": right_color}},
            "axisLabel": {"formatter": _AXIS_LABEL_FORMATTER, "color": "#8B97A8", "fontSize": 11},
            "splitLine": {"show": False},
        })
    else:
        yaxis_list.append({
            "type": "value",
            "axisLabel": {"formatter": _AXIS_LABEL_FORMATTER, "color": "#8B97A8", "fontSize": 11},
            "splitLine": {"lineStyle": {"color": "#EEF1F6", "type": "dashed"}},
        })

    # series
    series = []
    for i, m_col in enumerate(measure_cols):
        s = {
            "name": m_col,
            "type": "line" if (chart_type == "line" or is_area) else "bar",
            "data": measure_values[i],
            "itemStyle": {"color": _MULTI_MEASURE_COLORS[i % len(_MULTI_MEASURE_COLORS)]},
            "yAxisIndex": axis_assignments[i],
        }
        if is_stacked:
            s["stack"] = "total"
        if chart_type == "line" or is_area:
            s["smooth"] = True
            s["symbol"] = "circle"
            s["symbolSize"] = 6
        if is_area:
            s["areaStyle"] = {"opacity": 0.25}
        if chart_type == "bar":
            s["barMaxWidth"] = 40
        series.append(s)

    # xAxis
    if horizontal:
        xaxis = {
            "type": "value",
            "axisLabel": {"formatter": _AXIS_LABEL_FORMATTER, "color": "#8B97A8", "fontSize": 11},
            "splitLine": {"lineStyle": {"color": "#EEF1F6", "type": "dashed"}},
        }
    else:
        xaxis = {
            "type": "category",
            "data": dims,
            "axisLabel": {
                "color": "#8B97A8",
                "fontSize": 11,
                "interval": 0,
                "rotate": 30 if len(dims) > 6 else 0,
            },
            "axisLine": {"lineStyle": {"color": "#E2E8F0"}},
        }

    # yAxis output
    if horizontal:
        yaxis_out = {
            "type": "category",
            "data": list(reversed(dims)),
            "axisLabel": {"color": "#8B97A8", "fontSize": 11},
            "axisLine": {"show": False},
            "axisTick": {"show": False},
        }
    elif use_dual_axis:
        yaxis_out = yaxis_list
    else:
        yaxis_out = yaxis_list[0]

    # legend (PowerBI 风格)
    legend_data = [
        {"name": m, "icon": "roundRect", "textStyle": {"color": "#1A2332", "fontSize": 11}}
        for m in measure_cols
    ]

    grid_bottom = 28 if horizontal else 36
    grid_top = 40 if title else (32 if show_legend else 12)

    return {
        "title": (
            {"text": title, "left": "center", "textStyle": {"fontSize": 13, "fontWeight": 600, "color": "#1A2332"}}
            if title else None
        ),
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "shadow" if horizontal else "line"},
            "backgroundColor": "#FFFFFF",
            "borderColor": "#E2E8F0",
            "borderWidth": 1,
            "textStyle": {"color": "#1A2332", "fontSize": 12},
        },
        "legend": {
            "show": show_legend,
            "data": legend_data,
            "top": 6,
            "left": "center",
            "selectedMode": "multiple",
            "type": "scroll",
            "pageIconColor": "#8B97A8",
            "pageTextStyle": {"color": "#8B97A8"},
            "itemWidth": 12,
            "itemHeight": 8,
            "itemGap": 14,
        },
        "grid": {
            "top": grid_top,
            "left": 8,
            "right": 12 if use_dual_axis else 16,
            "bottom": grid_bottom,
            "containLabel": True,
        },
        "xAxis": xaxis,
        "yAxis": yaxis_out,
        "series": series,
    }


# ==================== 工具基类 ====================

class BaseTool(ABC):
    """工具基类，定义所有 Agent 工具的公共接口。

    所有具体工具（如 ListDatasourcesTool、QueryDatasourceTool、RenderChartTool）需继承此类并实现以下抽象方法。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """返回工具名称，用于工具路由和注册。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """返回工具描述，供 LLM 理解工具用途。"""

    @abstractmethod
    def schema(self) -> dict:
        """返回工具的 OpenAI function calling schema 定义。"""

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具逻辑，接收参数字典，返回 JSON 字符串结果。"""


class ToolRegistry:
    _tools: dict[str, BaseTool] = {}

    @classmethod
    def register(cls, tool: BaseTool) -> None:
        cls._tools[tool.name] = tool

    @classmethod
    def get(cls, name: str) -> BaseTool | None:
        return cls._tools.get(name)

    @classmethod
    def schemas(cls) -> list[dict]:
        return [t.schema() for t in cls._tools.values()]


# ==================== 工具实现 ====================

class ListDatasourcesTool(BaseTool):
    """浏览当前用户可用的所有数据源"""

    name = "list_datasources"
    description = "列出当前可用的所有数据源。返回每个数据源的 ID、名称、类型、字段列表和行数。"

    def schema(self) -> dict:
        """返回 list_datasources 工具的 OpenAI function calling schema。

        该工具无参数，调用时无需传入任何参数。

        返回：
            OpenAI function calling 格式的 schema 字典
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }

    async def execute(self, user_id: str, db_session=None, **kwargs) -> str:
        """执行列出数据源的操作，返回当前用户所有数据源的概要信息。

        从数据库中查询当前用户的数据源列表，为每个数据源提取字段信息、
        构建 table_ref（表引用）和 sample_sql（预览 SQL），最后以 JSON 格式返回。

        参数：
            user_id: 当前用户 ID，用于过滤数据源
            db_session: 数据库会话对象，用于异步查询（若为 None 则返回错误）
            **kwargs: 额外关键字参数（未使用）

        返回：
            JSON 字符串，包含数据源总数、总行数估计值、数据源详情列表以及查询提示
        """
        import uuid
        from app.models.datasource import SourceType
        from app.repositories.datasource_repository import SQLAlchemyDataSourceRepository

        if db_session is None:
            return json.dumps({"error": "数据库会话不可用"}, ensure_ascii=False)

        # 使用 Repository 查询（严格模式，要求 user_id 是合法 UUID）
        repo = SQLAlchemyDataSourceRepository(db_session)
        try:
            user_uuid = uuid.UUID(user_id)
            datasources, _ = await repo.list_datasources(
                user_id=user_uuid,
                page=1,
                page_size=100,
                source_type=None,
                status=None,
                search=None,
            )
        except ValueError:
            return json.dumps({"error": f"无效的用户 ID: {user_id}"}, ensure_ascii=False)

        summary = []
        for ds in datasources:
            schema_meta = ds.schema_meta or {}
            fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
            # columns: 纯列名数组，供 LLM 直接用于 SQL 生成
            columns = []
            # field_names: "name(type)" 格式，供 LLM 了解字段类型
            field_names = []
            for f in fields:
                if isinstance(f, dict):
                    name = f.get('name', '?')
                    dtype = f.get('data_type', '?')
                    columns.append(name)
                    field_names.append(f"{name}({dtype})")

            # 构建 table_ref，让 LLM 知道查询时 FROM 后面该写什么
            conn_cfg = dict(ds.connection_config) if ds.connection_config else {}
            schema_name = duckdb_client.get_schema_name(user_id, str(ds.id), ds.name, db_name=conn_cfg.get("db_name", ""))
            if ds.source_type in (SourceType.postgresql, SourceType.mysql):
                table_name = schema_meta.get("table_name", "data") if isinstance(schema_meta, dict) else "data"
                table_ref = f'"{schema_name}".public."{table_name}"'
            else:
                table_ref = f'"{schema_name}"."data"'

            # 构建可直接执行的预览 SQL
            sample_sql = f'SELECT * FROM {table_ref} LIMIT 1'

            summary.append({
                "id": str(ds.id),
                "name": ds.name,
                "description": ds.description,
                "type": ds.source_type.value if ds.source_type else "unknown",
                "row_count": ds.row_count,
                "columns": columns,
                "fields": field_names,
                "table_ref": table_ref,
                "sample_sql": sample_sql,
            })

        total = len(summary)
        total_rows = sum(d.get("row_count", 0) for d in summary if d.get("row_count"))

        return json.dumps({
            "total_datasources": total,
            "total_rows_estimate": total_rows,
            "datasources": summary,
            "tip": (
                "查询前建议先用 sample_sql 执行 SELECT * LIMIT 1 看实际列名和数据格式，"
                "然后用 columns 数组中的列名写正式的聚合查询。"
                "FROM 用 table_ref，列名用双引号包裹。"
            ),
        }, ensure_ascii=False)


class QueryDatasourceTool(BaseTool):
    """执行 SQL 查询（带三层安全防护）"""

    name = "query_datasource"
    description = (
        "在指定的数据源上执行 DuckDB SQL 查询（仅支持 SELECT）。"
        "传入 datasource_id 和 SQL 语句。返回前 50 行结果。"
        "重要：FROM 用 table_ref，列名用 list_datasources 返回的 columns 数组中的值并加双引号。"
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
                        "datasource_id": {
                            "type": "string",
                            "description": "数据源 ID（从 list_datasources 获取）",
                        },
                        "sql": {
                            "type": "string",
                            "description": "DuckDB SQL 查询（仅 SELECT）",
                        },
                    },
                    "required": ["datasource_id", "sql"],
                },
            },
        }

    async def execute(self, datasource_id: str, sql: str, user_id: str,
                      db_session=None, **kwargs) -> str:
        """在指定数据源上执行 SQL 查询并返回结果。

        流程：L3 安全校验 → 查询数据源元信息 → ATTACH 外部数据源（PostgreSQL/MySQL）→ 执行查询 → 解析列名 → 返回结果。
        查询失败时返回详细的纠错信息（table_ref、可用列名），供 LLM 自纠错。

        参数：
            datasource_id: 数据源 ID（从 list_datasources 获取）
            sql: 要执行的 DuckDB SQL 查询语句（仅支持 SELECT）
            user_id: 当前用户 ID，用于权限校验
            db_session: 数据库会话对象，用于查询数据源元信息
            **kwargs: 额外关键字参数（未使用）

        返回：
            JSON 字符串，成功时包含 columns（列名）、row_count（行数）、rows（数据行）；
            失败时包含 error 信息和纠错 hint
        """
        # L3 安全检查：通过 sql_guard 对 SQL 进行完整校验（拦截危险操作）
        guard: GuardResult = sql_guard.full_check("", sql)
        if not guard.allowed:
            return json.dumps({"error": guard.reason}, ensure_ascii=False)

        final_sql = guard.sanitized_sql or sql

        from sqlalchemy import select
        from app.models.datasource import DataSource, SourceType

        ds_result = await db_session.execute(
            select(DataSource).where(
                DataSource.id == datasource_id,
                DataSource.user_id == user_id,
            )
        )
        datasource = ds_result.scalar_one_or_none()
        if not datasource:
            return json.dumps({"error": "数据源不存在或无权限"}, ensure_ascii=False)

        conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
        schema_name = duckdb_client.get_schema_name(user_id, datasource_id, datasource.name, db_name=conn_info.get("db_name", ""))

        # 如果数据源是外部数据库（PostgreSQL/MySQL），先解密连接密码并 ATTACH 到 DuckDB
        if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
            from app.utils.crypto import decrypt_value, get_encryption_key
            # 先尝试 DETACH 旧的同名 schema，避免 ATTACH 冲突
            try:
                duckdb_client.execute(f'DETACH "{schema_name}"')
            except Exception:
                pass
            key = get_encryption_key()
            if key and conn_info.get("password"):
                conn_info["password"] = decrypt_value(conn_info["password"], key)
            conn_info["user"] = conn_info.get("username", "postgres")
            conn_info["database"] = conn_info.get("db_name", "")
            from app.connectors.postgres_connector import postgres_connector as pg_conn
            attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
            duckdb_client.execute(attach_sql)

        # 执行 SQL 查询并解析结果列名
        try:
            rows_raw = duckdb_client.fetchall(final_sql)
            cols = []
            # 通过正则从 SELECT 子句中提取列名（支持 AS 别名和双引号包裹）
            select_match = re.match(
                r"SELECT\s+(.+?)\s+FROM", final_sql,
                re.IGNORECASE | re.DOTALL,
            )
            if select_match:
                cols_str = select_match.group(1)
                # 按逗号分割列表达式（忽略括号内的逗号，如函数调用）
                col_parts = re.split(r",(?![^(]*\))", cols_str)
                for part in col_parts:
                    part = part.strip()
                    # 匹配 "column AS alias" 或 "column" 或带引号的列名
                    as_match = re.search(
                        r'(?:AS\s+)?["\']?(\w+)["\']?\s*$',
                        part, re.IGNORECASE,
                    )
                    if as_match:
                        cols.append(as_match.group(1))
                    else:
                        clean = part.strip('"').strip("'")
                        if "." in clean:
                            clean = clean.split(".")[-1]
                        cols.append(clean)
            # 如果正则提取失败但存在数据，生成默认列名
            if not cols and rows_raw:
                cols = [f"col_{i}" for i in range(len(rows_raw[0]))]

            def _safe(v):
                if v is None:
                    return None
                if isinstance(v, (int, float, str, bool)):
                    return v
                return str(v)

            data_rows = [[_safe(v) for v in row] for row in rows_raw[:50]]
            # summary：供 LLM 核对结果是否符合用户问题意图（语义自评）
            summary = {
                "columns_count": len(cols),
                "rows_count": len(data_rows),
                "sample": data_rows[:3],
            }
            return json.dumps({
                "columns": cols,
                "row_count": len(data_rows),
                "rows": data_rows,
                "summary": summary,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            log.warning("Query tool error: %s", e)
            # 构建 table_ref 让 AI 能直接用正确的 FROM 子句重试
            if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
                meta = datasource.schema_meta if isinstance(datasource.schema_meta, dict) else {}
                table_name = meta.get("table_name", "data")
                table_ref = f'"{schema_name}".public."{table_name}"'
            else:
                table_ref = f'"{schema_name}"."data"'

            # 把数据源真实列名和 table_ref 注入 hint，让 LLM 能立即自纠错
            available_columns: list[str] = []
            schema_meta = datasource.schema_meta or {}
            fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
            for f in fields:
                if isinstance(f, dict) and f.get("name"):
                    available_columns.append(str(f["name"]))
            hint_lines = [
                f"FROM 子句必须用: {table_ref}（直接复制这个字符串，不要修改）",
                "列名必须加双引号。",
                "以下为该数据源真实列名（必须一字不差从这里复制）：",
                ", ".join(f'"{c}"' for c in available_columns),
            ]
            return json.dumps({
                "error": f"查询执行失败: {str(e)[:200]}",
                "attempted_sql": final_sql[:300],
                "table_ref": table_ref,
                "hint": "\n".join(hint_lines),
                "available_columns": available_columns,
            }, ensure_ascii=False)


class RenderChartTool(BaseTool):
    """生成 ECharts 图表配置"""

    name = "render_chart"
    description = (
        "根据数据生成 ECharts 图表配置。传入 chart_type 和数据。"
        "返回 ECharts option 配置对象。"
        "支持的图表类型: bar(柱状图), line(折线图), pie(饼图), donut(环形图), "
        "area(面积图), scatter(散点图), kpi_card(指标卡), grouped_bar(分组柱状图), "
        "stacked_bar(堆叠柱状图), horizontal_bar(水平条形图), "
        "heatmap(热力图), radar(雷达图), funnel(漏斗图)"
    )

    def schema(self) -> dict:
        """返回 render_chart 工具的 OpenAI function calling schema。

        需要四个参数：chart_type（图表类型）、title（图表标题）、columns（数据列名列表）、
        rows（数据行二维数组）。

        返回：
            OpenAI function calling 格式的 schema 字典
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chart_type": {
                            "type": "string",
                            "enum": [
                                "bar", "line", "pie", "donut", "area", "scatter",
                                "kpi_card", "grouped_bar", "stacked_bar",
                                "horizontal_bar",
                                "heatmap", "radar", "funnel",
                            ],
                            "description": "图表类型",
                        },
                        "title": {
                            "type": "string",
                            "description": "图表标题",
                        },
                        "columns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "数据列名列表",
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                            "description": "数据行（二维数组）",
                        },
                    },
                    "required": ["chart_type", "title", "columns", "rows"],
                },
            },
        }

    async def execute(self, chart_type: str, title: str, columns: list,
                      rows: list, **kwargs) -> str:
        """根据传入的图表类型和数据生成 ECharts 图表 option 配置。

        支持多种图表类型：普通柱状图、折线图、饼图、环形图、面积图、散点图、指标卡、
        分组柱状图、堆叠柱状图、水平条形图、热力图、雷达图、漏斗图。
        对于多度量场景（2+ 个度量值），走双Y轴构建器 _build_multi_measure_option，
        与前端 buildMultiMeasureOption 行为一致。

        参数：
            chart_type: 图表类型字符串
            title: 图表标题
            columns: 数据列名列表，第一个为维度列，后续为度量列
            rows: 数据行二维数组
            **kwargs: 额外关键字参数（未使用）

        返回：
            JSON 字符串，包含原始 chart_type 和 ECharts option 配置对象
        """
        if not rows or not columns:
            return json.dumps({"error": "数据为空"}, ensure_ascii=False)

        # 自校验：图表类型 + 数据一致性（内嵌校验，失败信息供 LLM 自纠错）
        pre_check = validate_chart_option(chart_type, columns, rows, None)
        if not pre_check["valid"]:
            return json.dumps({
                "error": "；".join(pre_check["errors"]),
                "hint": pre_check["hint"],
            }, ensure_ascii=False)

        axisless = ("pie", "donut", "kpi_card", "funnel")
        # 多度量场景（columns 长度 ≥ 3 = 1 维度 + 2+ 度量）走双Y轴 builder
        # 镜像前端 buildMultiMeasureOption，保证 AI 推荐链路和画布手动配置一致
        n_measures = max(0, len(columns) - 1)
        use_multi_measure_builder = (
            chart_type in ("bar", "line", "area", "stacked_bar",
                           "grouped_bar", "horizontal_bar")
            and n_measures >= 2
        )

        if use_multi_measure_builder:
            horizontal = chart_type == "horizontal_bar"
            stacked = chart_type == "stacked_bar"
            # grouped_bar / stacked_bar / horizontal_bar 都基于 bar 构建
            base_type = "bar" if chart_type in ("stacked_bar", "grouped_bar",
                                                "horizontal_bar") else chart_type
            option = _build_multi_measure_option(
                chart_type=base_type,
                columns=columns,
                rows=rows,
                title=title,
                horizontal=horizontal,
                stacked=stacked,
            )
            post_check = validate_chart_option(chart_type, columns, rows, option)
            if not post_check["valid"]:
                return json.dumps({
                    "error": "；".join(post_check["errors"]),
                    "hint": post_check["hint"],
                }, ensure_ascii=False)
            return json.dumps({
                "chart_type": chart_type,
                "option": option,
            }, ensure_ascii=False)

        option = {
            "title": {"text": title, "left": "center"},
            "tooltip": {"trigger": "axis" if chart_type not in axisless else "item"},
        }

        if chart_type in ("kpi_card",):
            val = rows[0][1] if len(rows[0]) > 1 else rows[0][0]
            option["series"] = [{
                "type": "gauge",
                "data": [{"value": val, "name": columns[1] if len(columns) > 1 else columns[0]}],
                "detail": {"formatter": "{value}"},
            }]

        elif chart_type in ("pie", "donut"):
            option["series"] = [{
                "type": "pie",
                "radius": ["40%", "70%"] if chart_type == "donut" else "70%",
                "data": [{"name": str(r[0]), "value": r[1]} for r in rows],
            }]

        elif chart_type == "funnel":
            # 漏斗图：第一列阶段名，第二列数值（降序排列）
            funnel_data = sorted(
                [{"name": str(r[0]), "value": r[1]} for r in rows if len(r) > 1],
                key=lambda x: x["value"], reverse=True,
            )
            option["series"] = [{
                "type": "funnel",
                "data": funnel_data,
                "sort": "descending",
                "gap": 2,
                "label": {"show": True, "position": "inside"},
            }]

        elif chart_type == "radar":
            # 雷达图：第一列是维度名，后续每一列是一个指标
            indicator = [{"name": str(r[0]), "max": max(r[1:] or [1]) * 1.2} for r in rows]
            values = [r[1] if len(r) > 1 else 0 for r in rows]
            option["radar"] = {"indicator": indicator}
            option["series"] = [{
                "type": "radar",
                "data": [{"value": values, "name": title}],
            }]

        elif chart_type == "heatmap":
            # 热力图：每行 [x, y, value]，需要去重构建坐标轴
            x_set = sorted(set(str(r[0]) for r in rows if len(r) > 2))
            y_set = sorted(set(str(r[1]) for r in rows if len(r) > 2))
            heat_data = [[str(r[0]), str(r[1]), r[2]] for r in rows if len(r) > 2]
            option["xAxis"] = {"type": "category", "data": x_set, "splitArea": {"show": True}}
            option["yAxis"] = {"type": "category", "data": y_set, "splitArea": {"show": True}}
            option["visualMap"] = {
                "min": min((r[2] for r in heat_data), default=0),
                "max": max((r[2] for r in heat_data), default=100),
                "calculable": True,
                "orient": "horizontal",
                "left": "center",
                "bottom": "0%",
            }
            option["series"] = [{
                "type": "heatmap",
                "data": heat_data,
                "label": {"show": True},
            }]
            # heatmap 使用 category 轴，tooltip 改为 item
            option["tooltip"] = {"trigger": "item"}

        elif chart_type in ("grouped_bar", "stacked_bar"):
            # 分组/堆叠柱状图：第1列是维度，后续每列是一个 series
            dims = [str(r[0]) for r in rows]
            series_list = []
            for col_idx in range(1, len(columns)):
                series_data = [r[col_idx] if len(r) > col_idx else 0 for r in rows]
                series_list.append({
                    "type": "bar",
                    "name": columns[col_idx],
                    "data": series_data,
                    "stack": "total" if chart_type == "stacked_bar" else None,
                })
            option["xAxis"] = {"type": "category", "data": dims}
            option["yAxis"] = {"type": "value"}
            option["legend"] = {"data": columns[1:]}
            option["series"] = series_list

        else:
            dims = [str(r[0]) for r in rows]
            series_data = [r[1] if len(r) > 1 else 0 for r in rows]
            series_name = columns[1] if len(columns) > 1 else "value"

            if chart_type == "bar":
                option["xAxis"] = {"type": "category", "data": dims}
                option["yAxis"] = {"type": "value"}
                option["series"] = [{"type": "bar", "data": series_data, "name": series_name}]
            elif chart_type == "horizontal_bar":
                # 水平条形：x/y 轴对调，让类目在纵轴、数值在横轴
                option["xAxis"] = {"type": "value"}
                option["yAxis"] = {"type": "category", "data": list(reversed(dims))}
                option["series"] = [{
                    "type": "bar",
                    "data": list(reversed(series_data)),
                    "name": series_name,
                }]
            elif chart_type == "line":
                option["xAxis"] = {"type": "category", "data": dims}
                option["yAxis"] = {"type": "value"}
                option["series"] = [{
                    "type": "line", "data": series_data,
                    "name": series_name,
                }]
            elif chart_type == "area":
                option["xAxis"] = {"type": "category", "data": dims}
                option["yAxis"] = {"type": "value"}
                option["series"] = [{
                    "type": "line", "data": series_data,
                    "name": series_name, "areaStyle": {},
                }]
            elif chart_type == "scatter":
                scatter_data = [[r[0], r[1]] for r in rows if len(r) > 1]
                option["xAxis"] = {"type": "value", "name": columns[0] if columns else ""}
                option["yAxis"] = {"type": "value", "name": columns[1] if len(columns) > 1 else ""}
                option["series"] = [{"type": "scatter", "data": scatter_data}]

        post_check = validate_chart_option(chart_type, columns, rows, option)
        if not post_check["valid"]:
            return json.dumps({
                "error": "；".join(post_check["errors"]),
                "hint": post_check["hint"],
            }, ensure_ascii=False)
        return json.dumps({
            "chart_type": chart_type,
            "option": option,
        }, ensure_ascii=False)



# ==================== 图表配置校验（共享校验器） ====================
# 单一校验实现：ValidateChartTool（工具）、render_chart（内嵌）、画布助手（API 层复用）三处共用

VALID_CHART_TYPES: frozenset[str] = frozenset({
    "bar", "line", "pie", "donut", "area", "scatter", "kpi_card",
    "grouped_bar", "stacked_bar", "horizontal_bar",
    "funnel", "heatmap", "radar", "sankey",
})


def _validate_chart_type(ct: str) -> str | None:
    """校验图表类型是否在白名单内，非法返回错误信息，合法返回 None。"""
    if ct not in VALID_CHART_TYPES:
        return f"不支持的图表类型: {ct}，支持: {', '.join(sorted(VALID_CHART_TYPES))}"
    return None


def validate_chart_config(config: dict, available_fields: set[str] | None = None) -> dict:
    """校验画布配置形态：{action, chart_type, dimensions, measures, filters, rationale}。

    与画布助手 API 层校验逻辑等价，作为唯一实现供两端复用。

    返回：
        {"valid": bool, "errors": list, "hint": str}
    """
    errors: list[str] = []
    if not isinstance(config, dict):
        return {"valid": False, "errors": ["配置必须是 JSON 对象"], "hint": "请输出正确的图表配置 JSON"}
    ct = config.get("chart_type") or config.get("chartType") or ""
    if ct:
        err = _validate_chart_type(str(ct))
        if err:
            errors.append(err)
    if available_fields:
        dims = config.get("dimensions") or []
        meas = config.get("measures") or []
        if not isinstance(dims, list) or not isinstance(meas, list):
            errors.append("dimensions 和 measures 必须是数组")
        else:
            meas_fields = [m.get("field") if isinstance(m, dict) else m for m in meas]
            unknown = [
                d for d in dims
                if d not in available_fields and d.lower() not in available_fields
            ] + [
                m for m in meas_fields
                if m not in available_fields and m.lower() not in available_fields
            ]
            if unknown:
                errors.append(f"引用了数据源中不存在的字段: {', '.join(str(u) for u in unknown)}")
    if errors:
        return {"valid": False, "errors": errors, "hint": "请使用数据源真实字段名，图表类型用英文代码（如 bar/line/pie）"}
    return {"valid": True, "errors": [], "hint": ""}


def validate_chart_option(chart_type: str | None, columns: list | None, rows: list | None, option: dict | None) -> dict:
    """校验 render_chart 产物形态：{chart_type, columns, rows, option}。

    只拦结构性错误（类型非法 / 数据错位 / option 缺失关键结构），不拦风格差异。

    返回：
        {"valid": bool, "errors": list, "hint": str}
    """
    errors: list[str] = []
    if chart_type:
        err = _validate_chart_type(chart_type)
        if err:
            errors.append(err)
    # 数据一致性：rows 每行列数与 columns 对齐、数值列可转 float
    if columns is not None and rows is not None:
        if not isinstance(columns, list) or not isinstance(rows, list):
            errors.append("columns 和 rows 必须是数组")
        elif rows:
            for i, row in enumerate(rows[:10]):
                if not isinstance(row, (list, tuple)) or len(row) != len(columns):
                    errors.append(f"第 {i + 1} 行数据列数({len(row) if isinstance(row, (list, tuple)) else '?'})与 columns 列数({len(columns)})不一致")
                    break
    # option 结构完整性
    if option is not None:
        if not isinstance(option, dict):
            errors.append("option 必须是 JSON 对象")
        else:
            series = option.get("series")
            if not series:
                errors.append("option 缺少 series 或 series 为空")
            elif isinstance(series, list):
                for s in series:
                    if not isinstance(s, dict):
                        errors.append("series 元素必须是对象")
                    elif "data" not in s:
                        errors.append("series 缺少 data 字段")
            else:
                errors.append("series 必须是数组")
    if errors:
        return {"valid": False, "errors": errors, "hint": "请根据错误信息修复图表配置后重新调用 render_chart"}
    return {"valid": True, "errors": [], "hint": ""}


class ValidateChartTool(BaseTool):
    """校验图表配置的合法性（自校验工具，供 Agent 与画布助手复用）"""

    name = "validate_chart"
    description = (
        "校验图表配置是否合法：图表类型白名单、字段引用是否存在、数据行列是否对齐、"
        "ECharts option 结构是否完整。返回 valid + 问题列表 + 修复建议。"
        "支持两种输入：1) config 形态（chart_type/dimensions/measures/filters）"
        "2) option 形态（chart_type/columns/rows/option）。"
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
                        "chart_type": {"type": "string", "description": "图表类型（英文代码）"},
                        "columns": {"type": "array", "items": {"type": "string"}, "description": "数据列名列表"},
                        "rows": {"type": "array", "items": {"type": "array"}, "description": "数据行（二维数组）"},
                        "option": {"type": "object", "description": "ECharts option 配置对象"},
                        "config": {"type": "object", "description": "画布图表配置：{action, chart_type, dimensions, measures, filters}"},
                    },
                    "required": [],
                },
            },
        }

    async def execute(self, chart_type=None, columns=None, rows=None, option=None,
                      config=None, available_fields=None, **kwargs) -> str:
        try:
            if config is not None:
                fields_set = None
                if isinstance(available_fields, list):
                    fields_set = {str(f) for f in available_fields}
                result = validate_chart_config(config, fields_set)
            else:
                result = validate_chart_option(chart_type, columns, rows, option)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"valid": False, "errors": [str(e)], "hint": "校验器内部异常"}, ensure_ascii=False)


# ==================== 结构化安全查询工具 ====================

class QueryEngineTool(BaseTool):
    """通过查询引擎执行结构化查询（参数化 SQL，比原生 SQL 更安全）"""

    name = "query_engine"
    description = (
        "结构化安全查询：传入 datasource_id、dimensions（维度）、measures（度量，含聚合方式）、"
        "filters（过滤条件）、sort（排序）、limit，由查询引擎生成参数化 SQL 执行。"
        "比 query_datasource 更安全（字段白名单 + 参数化），但灵活性低，适合标准聚合分析。"
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
                        "datasource_id": {"type": "string", "description": "数据源 ID（从 list_datasources 获取）"},
                        "dimensions": {"type": "array", "items": {"type": "string"}, "description": "维度字段名列表"},
                        "measures": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"field": {"type": "string"}, "agg": {"type": "string", "enum": ["SUM", "AVG", "COUNT", "COUNT_DISTINCT", "MIN", "MAX"]}},
                                "required": ["field", "agg"],
                            },
                            "description": "度量列表 [{field, agg}]",
                        },
                        "filters": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "过滤条件 [{field, op(eq/neq/gt/gte/lt/lte/like/in/between), value}]",
                        },
                        "sort": {"type": "object", "description": "排序 {field, order(desc/asc)}"},
                        "limit": {"type": "integer", "description": "返回行数上限（默认 50）"},
                    },
                    "required": ["datasource_id"],
                },
            },
        }

    async def execute(self, datasource_id: str, dimensions=None, measures=None, filters=None,
                      sort=None, limit: int = 50, user_id: str | None = None,
                      db_session=None, **kwargs) -> str:
        try:
            from app.schemas.query import ChartQueryConfig, FilterConfig, MeasureConfig, SortConfig
            from app.services.query_engine import execute_chart_query

            meas_objs = []
            for m in (measures or []):
                if isinstance(m, dict):
                    meas_objs.append(MeasureConfig(field=str(m.get("field", "")), agg=str(m.get("agg", "SUM"))))
                elif isinstance(m, str):
                    meas_objs.append(MeasureConfig(field=m, agg="SUM"))
            filt_objs = []
            for f in (filters or []):
                if isinstance(f, dict):
                    filt_objs.append(FilterConfig(field=str(f.get("field", "")), op=str(f.get("op", "eq")), value=f.get("value")))
            sort_obj = SortConfig(field=str(sort.get("field", "")), order=str(sort.get("order", "desc"))) if isinstance(sort, dict) and sort.get("field") else None

            config = ChartQueryConfig(
                dimensions=list(dimensions or []),
                measures=meas_objs,
                filters=filt_objs,
                chart_type=None,
                sort=sort_obj,
                limit=max(1, min(int(limit or 50), 1000)),
            )
            result = await execute_chart_query(
                datasource_id=datasource_id,
                config=config,
                user_id=user_id or "",
                db=db_session,
            )
            return json.dumps({
                "columns": result.columns,
                "row_count": len(result.rows),
                "rows": result.rows[:50],
                "summary": {
                    "columns_count": len(result.columns),
                    "rows_count": len(result.rows),
                    "sample": result.rows[:3],
                },
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"结构化查询失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 数据质量工具 ====================

class DataQualityTool(BaseTool):
    """数据质量分析：检查缺失值、异常值、重复行、类型不一致、格式问题"""

    name = "data_quality"
    description = (
        "数据质量分析：对指定数据源执行质量检查（缺失值、IQR 异常值、重复行、"
        "类型不一致、格式问题）。可传 fields 限定检查范围，不传则检查全部字段。"
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
                        "datasource_id": {"type": "string", "description": "数据源 ID"},
                        "fields": {"type": "array", "items": {"type": "string"}, "description": "要检查的字段名列表（可选，默认全部）"},
                    },
                    "required": ["datasource_id"],
                },
            },
        }

    async def execute(self, datasource_id: str, fields=None, user_id: str | None = None,
                      db_session=None, **kwargs) -> str:
        try:
            from app.services import data_quality as dq

            target_fields = list(fields) if fields else None
            if not target_fields and db_session is not None:
                # 未指定字段时从数据源 schema_meta 取全部字段
                from sqlalchemy import select
                from app.models.datasource import DataSource
                res = await db_session.execute(select(DataSource).where(DataSource.id == datasource_id))
                ds = res.scalar_one_or_none()
                if ds and isinstance(ds.schema_meta, dict):
                    target_fields = [f.get("name") for f in (ds.schema_meta.get("fields") or []) if isinstance(f, dict) and f.get("name")]

            report: list[dict] = []
            if target_fields:
                for field in target_fields:
                    try:
                        null_res = await dq.null_count(datasource_id, field, db_session)
                        if null_res.get("count", 0) > 0:
                            report.append({**null_res, "field": field, "issue_type": "missing"})
                    except Exception:
                        pass
                    try:
                        outlier_res = await dq.outlier_iqr_count(datasource_id, field, db_session)
                        if outlier_res.get("count", 0) > 0:
                            report.append({**outlier_res, "field": field, "issue_type": "outlier_iqr"})
                    except Exception:
                        pass
                    try:
                        type_res = await dq.type_inconsistency_count(datasource_id, field, db_session)
                        if type_res.get("count", 0) > 0:
                            report.append({**type_res, "field": field, "issue_type": "type_inconsistency"})
                    except Exception:
                        pass
                    try:
                        fmt_res = await dq.format_issue_count(datasource_id, field, db_session)
                        if fmt_res.get("count", 0) > 0:
                            report.append({**fmt_res, "field": field, "issue_type": "format"})
                    except Exception:
                        pass
            try:
                dup_res = await dq.dup_row_count(datasource_id, db_session)
                if dup_res.get("count", 0) > 0:
                    report.append({**dup_res, "field": "*", "issue_type": "duplicate"})
            except Exception:
                pass

            return json.dumps({
                "checked_fields": target_fields or [],
                "issue_count": len(report),
                "issues": report,
                "summary": f"共发现 {len(report)} 项数据质量问题" if report else "未发现明显数据质量问题",
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"数据质量分析失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 洞察工具 ====================

class InsightTool(BaseTool):
    """自动洞察：聚合查询 + LLM 生成趋势/异常洞察"""

    name = "insight"
    description = (
        "自动洞察：对数据源执行聚合分析（按维度/度量），并生成趋势、异常、分布等数据洞察。"
        "适合用户询问'有什么趋势/异常/发现'的场景。"
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
                        "datasource_id": {"type": "string", "description": "数据源 ID"},
                        "dimensions": {"type": "array", "items": {"type": "string"}, "description": "维度字段"},
                        "measures": {"type": "array", "items": {"type": "object"}, "description": "度量 [{field, agg}]"},
                        "filters": {"type": "array", "items": {"type": "object"}, "description": "过滤条件"},
                    },
                    "required": ["datasource_id"],
                },
            },
        }

    async def execute(self, datasource_id: str, dimensions=None, measures=None, filters=None,
                      user_id: str | None = None, db_session=None, **kwargs) -> str:
        try:
            from app.schemas.query import ChartQueryConfig, FilterConfig, MeasureConfig
            from app.services.query_engine import execute_chart_query
            from app.services.ai_service import AIService

            dims = list(dimensions or [])
            meas_objs = []
            for m in (measures or []):
                if isinstance(m, dict):
                    meas_objs.append(MeasureConfig(field=str(m.get("field", "")), agg=str(m.get("agg", "SUM"))))
                elif isinstance(m, str):
                    meas_objs.append(MeasureConfig(field=m, agg="SUM"))
            if not meas_objs:
                # 未指定度量时，从数据源取第一个数值字段作为默认度量
                from sqlalchemy import select
                from app.models.datasource import DataSource
                res = await db_session.execute(select(DataSource).where(DataSource.id == datasource_id))
                ds = res.scalar_one_or_none()
                if ds and isinstance(ds.schema_meta, dict):
                    for f in (ds.schema_meta.get("fields") or []):
                        if isinstance(f, dict) and "int" in str(f.get("data_type", "")).lower():
                            meas_objs.append(MeasureConfig(field=str(f.get("name")), agg="SUM"))
                            break

            config = ChartQueryConfig(
                dimensions=dims,
                measures=meas_objs,
                filters=[FilterConfig(field=str(f.get("field", "")), op=str(f.get("op", "eq")), value=f.get("value")) for f in (filters or []) if isinstance(f, dict)],
                chart_type=None,
                limit=500,
            )
            result = await execute_chart_query(
                datasource_id=datasource_id, config=config,
                user_id=user_id or "", db=db_session,
            )
            query_config = {"dimensions": dims, "measures": [{"field": m.field, "agg": m.agg} for m in meas_objs]}
            insights = await AIService().generate_insights(result.rows[:500], query_config)
            return json.dumps({
                "row_count": len(result.rows),
                "insights": insights,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"洞察生成失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 清洗建议工具 ====================

class CleanSuggestTool(BaseTool):
    """数据清洗建议：质量检查 + LLM 生成清洗建议"""

    name = "clean_suggest"
    description = (
        "数据清洗建议：对数据源执行质量检查，并生成按严重程度排序的中文清洗建议。"
        "适合用户询问'数据有什么问题/需要怎么清洗'的场景。"
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
                        "datasource_id": {"type": "string", "description": "数据源 ID"},
                    },
                    "required": ["datasource_id"],
                },
            },
        }

    async def execute(self, datasource_id: str, user_id: str | None = None,
                      db_session=None, **kwargs) -> str:
        try:
            from sqlalchemy import select
            from app.models.datasource import DataSource
            from app.services import data_quality as dq
            from app.services.ai_service import AIService

            res = await db_session.execute(select(DataSource).where(DataSource.id == datasource_id))
            ds = res.scalar_one_or_none()
            if not ds:
                return json.dumps({"error": "数据源不存在"}, ensure_ascii=False)

            field_meta = []
            fields: list[str] = []
            if isinstance(ds.schema_meta, dict):
                for f in (ds.schema_meta.get("fields") or []):
                    if isinstance(f, dict) and f.get("name"):
                        field_meta.append(f)
                        fields.append(str(f["name"]))

            stats: list[dict] = []
            for field in fields:
                try:
                    n = await dq.null_count(datasource_id, field, db_session)
                    if n.get("count", 0) > 0:
                        stats.append({"field": field, "issue_type": "missing", **{k: v for k, v in n.items() if k != "field"}})
                except Exception:
                    pass
                try:
                    o = await dq.outlier_iqr_count(datasource_id, field, db_session)
                    if o.get("count", 0) > 0:
                        stats.append({"field": field, "issue_type": "outlier", **{k: v for k, v in o.items() if k != "field"}})
                except Exception:
                    pass
            try:
                d = await dq.dup_row_count(datasource_id, db_session)
                if d.get("count", 0) > 0:
                    stats.append({"field": "*", "issue_type": "duplicate", **{k: v for k, v in d.items() if k != "field"}})
            except Exception:
                pass

            suggestions = await AIService().clean_suggest(field_meta, stats)
            return json.dumps({
                "checked_fields": fields,
                "suggestion_count": len(suggestions),
                "suggestions": suggestions,
            }, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"清洗建议生成失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 图表推荐工具 ====================

class RecommendChartsTool(BaseTool):
    """图表类型推荐：根据字段特征推荐最合适的图表类型"""

    name = "recommend_charts"
    description = (
        "图表类型推荐：根据数据源字段特征推荐 1-3 个最合适的图表类型，"
        "返回 chart_type + rationale + config。生成图表前可用它辅助选型。"
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
                        "datasource_id": {"type": "string", "description": "数据源 ID"},
                        "current_config": {"type": "object", "description": "当前图表配置（可选）：{dimensions, measures, chartType}"},
                    },
                    "required": ["datasource_id"],
                },
            },
        }

    async def execute(self, datasource_id: str, current_config=None,
                      user_id: str | None = None, db_session=None, **kwargs) -> str:
        try:
            from sqlalchemy import select
            from app.models.datasource import DataSource
            from app.services.ai_service import AIService

            res = await db_session.execute(select(DataSource).where(DataSource.id == datasource_id))
            ds = res.scalar_one_or_none()
            if not ds:
                return json.dumps({"error": "数据源不存在"}, ensure_ascii=False)
            field_meta = []
            if isinstance(ds.schema_meta, dict):
                field_meta = [f for f in (ds.schema_meta.get("fields") or []) if isinstance(f, dict)]

            suggestions = await AIService().recommend_charts(
                field_meta=field_meta,
                current_config=current_config or {},
                user_id=user_id,
            )
            return json.dumps({"recommendations": suggestions}, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": f"图表推荐失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 文本润色工具 ====================

class PolishTextTool(BaseTool):
    """文本润色：4 种风格转换"""

    name = "polish_text"
    description = (
        "文本润色：将文本转换为指定风格（professional 专业 / casual 轻松 / concise 简洁 / academic 学术）。"
        "适合用户要求改写或润色报告文本的场景。"
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
                        "text": {"type": "string", "description": "要润色的文本"},
                        "style": {"type": "string", "enum": ["professional", "casual", "concise", "academic"], "description": "目标风格"},
                    },
                    "required": ["text"],
                },
            },
        }

    async def execute(self, text: str, style: str = "professional", **kwargs) -> str:
        try:
            from app.services.ai_service import AIService
            result = await AIService().polish_text(text, style)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"润色失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 统计分析工具 ====================

def _numeric_values(values: list) -> list[float]:
    """把混合值列表转成可统计的数值列表（过滤 None/空串/不可转数值/布尔）。"""
    out: list[float] = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _quantile(sorted_vals: list[float], q: float) -> float:
    """线性插值分位数（无 numpy 依赖）。"""
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


class StatsAnalyzerTool(BaseTool):
    """统计分析：对查询结果（columns + rows）做描述性统计，数据可从依赖步骤自动填充"""

    name = "stats_analyzer"
    description = (
        "统计分析：对已有查询结果做描述性统计——数值列输出 count/mean/std/min/25%分位/中位数/75%分位/max/"
        "缺失数/异常值提示（IQR 法），类别列输出 Top N 频次与占比。"
        "适合用户问'统计特征/分布/平均值/中位数/异常值/数据概览'的场景。"
        "数据（columns+rows）从依赖的查询步骤结果自动填充，无需重新查询。"
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
                        "columns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "数据列名列表",
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                            "description": "数据行（二维数组，与 columns 对齐）",
                        },
                        "title": {
                            "type": "string",
                            "description": "统计主题（可选，用于标注统计目的）",
                        },
                    },
                    "required": ["columns", "rows"],
                },
            },
        }

    async def execute(self, columns=None, rows=None, title: str = "", **kwargs) -> str:
        try:
            cols = list(columns or [])
            rows_2d = list(rows or [])
            if not cols or not rows_2d:
                return json.dumps({
                    "error": "数据为空，请先通过 query_datasource/query_engine 查询获取数据",
                    "hint": "先执行查询步骤，再让本步骤依赖该查询步骤的结果",
                }, ensure_ascii=False)
            # 内嵌校验：行宽与列数对齐
            for i, row in enumerate(rows_2d[:10]):
                if not isinstance(row, (list, tuple)) or len(row) != len(cols):
                    return json.dumps({
                        "error": f"第 {i + 1} 行数据列数({len(row) if isinstance(row, (list, tuple)) else '?'})与 columns 列数({len(cols)})不一致",
                        "hint": "请检查 columns/rows 是否与查询结果一致",
                    }, ensure_ascii=False)

            n_rows = len(rows_2d)
            stats: list[dict] = []
            total_nulls = 0
            for ci, col in enumerate(cols):
                col_values = [row[ci] if ci < len(row) else None for row in rows_2d]
                nulls = sum(1 for v in col_values if v is None or (isinstance(v, str) and not v.strip()))
                total_nulls += nulls
                numeric = _numeric_values(col_values)
                if len(numeric) >= max(2, int(len(col_values) * 0.6)):
                    sv = sorted(numeric)
                    q1 = _quantile(sv, 0.25)
                    q3 = _quantile(sv, 0.75)
                    iqr = q3 - q1
                    lo_b, hi_b = q1 - 1.5 * iqr, q3 + 1.5 * iqr
                    outliers = [v for v in sv if v < lo_b or v > hi_b]
                    mean = sum(sv) / len(sv)
                    variance = sum((x - mean) ** 2 for x in sv) / len(sv)
                    stats.append({
                        "column": col,
                        "type": "numeric",
                        "count": len(col_values),
                        "non_null": len(col_values) - nulls,
                        "null_count": nulls,
                        "mean": round(mean, 4),
                        "std": round(variance ** 0.5, 4),
                        "min": round(sv[0], 4),
                        "p25": round(q1, 4),
                        "median": round(_quantile(sv, 0.5), 4),
                        "p75": round(q3, 4),
                        "max": round(sv[-1], 4),
                        "outlier_count": len(outliers),
                        "outlier_share": round(len(outliers) / len(sv), 4),
                    })
                else:
                    from collections import Counter
                    cnt = Counter(str(v) if v is not None else "(空)" for v in col_values)
                    top = cnt.most_common(5)
                    stats.append({
                        "column": col,
                        "type": "categorical",
                        "count": len(col_values),
                        "null_count": nulls,
                        "unique_count": len(cnt),
                        "top": [
                            {"value": v, "count": c, "share": round(c / len(col_values), 4) if col_values else 0.0}
                            for v, c in top
                        ],
                    })

            return json.dumps({
                "title": title or "统计分析",
                "row_count": n_rows,
                "column_count": len(cols),
                "null_total": total_nulls,
                "columns_stats": stats,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"统计分析失败: {str(e)[:200]}"}, ensure_ascii=False)


# ==================== 注册工具 ====================

list_datasources_tool = ListDatasourcesTool()
query_datasource_tool = QueryDatasourceTool()
render_chart_tool = RenderChartTool()
validate_chart_tool = ValidateChartTool()
query_engine_tool = QueryEngineTool()
data_quality_tool = DataQualityTool()
insight_tool = InsightTool()
clean_suggest_tool = CleanSuggestTool()
recommend_charts_tool = RecommendChartsTool()
polish_text_tool = PolishTextTool()
stats_analyzer_tool = StatsAnalyzerTool()

ToolRegistry.register(list_datasources_tool)
ToolRegistry.register(query_datasource_tool)
ToolRegistry.register(render_chart_tool)
ToolRegistry.register(validate_chart_tool)
ToolRegistry.register(query_engine_tool)
ToolRegistry.register(data_quality_tool)
ToolRegistry.register(insight_tool)
ToolRegistry.register(clean_suggest_tool)
ToolRegistry.register(recommend_charts_tool)
ToolRegistry.register(polish_text_tool)
ToolRegistry.register(stats_analyzer_tool)

