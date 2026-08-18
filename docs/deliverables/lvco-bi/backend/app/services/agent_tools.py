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
    """对话阶段：控制每个阶段可用的工具，防止跨阶段调用。"""
    SELECTING = "selecting"      # 选数据源 → 只暴露 list_datasources
    ANALYZING = "analyzing"      # 查数据   → 只暴露 query_datasource
    GENERATING = "generating"    # 生图表   → 只暴露 render_chart
    REPORTING = "reporting"      # 出报告   → 无工具，纯文本


def get_tools_for_phase(phase: ConversationPhase, all_schemas: list[dict]) -> list[dict]:
    """根据当前对话阶段过滤可用工具。"""
    if phase == ConversationPhase.SELECTING:
        return [s for s in all_schemas if s["function"]["name"] == "list_datasources"]
    elif phase == ConversationPhase.ANALYZING:
        # query_datasource + list_datasources（查询失败时自纠错需要）
        return [s for s in all_schemas if s["function"]["name"] in ("query_datasource", "list_datasources")]
    elif phase == ConversationPhase.GENERATING:
        return [s for s in all_schemas if s["function"]["name"] == "render_chart"]
    else:  # REPORTING
        return []


# ==================== AI 推荐图表 ECharts option 构建 ====================
# 镜像前端 buildMultiMeasureOption（echartsUtils.ts），保证双Y轴/图例/水平条形等
# 在 AI 推荐链路和画布手动配置链路下行为一致。
_MULTI_MEASURE_COLORS = [
    '#2BB5A0', '#6C7BF2', '#F5A623', '#EF5B5B',
    '#4EADFF', '#A78BFA', '#F472B6', '#34D399',
]


def _fmt_y(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if v >= 1000000:
        return f"{v / 1000000:.1f}M"
    if v >= 10000:
        return f"{v / 10000:.1f}w"
    if v >= 1000:
        return f"{v / 1000:.1f}k"
    return str(v)


def _safe_float(v, default=0.0):
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
    """Python 版 multi_measure ECharts option 构建器（双Y轴 + PowerBI 图例 + 水平条形）。"""
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
            "axisLabel": {"formatter": _fmt_y, "color": "#8B97A8", "fontSize": 11},
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
            "axisLabel": {"formatter": _fmt_y, "color": "#8B97A8", "fontSize": 11},
            "splitLine": {"show": False},
        })
    else:
        yaxis_list.append({
            "type": "value",
            "axisLabel": {"formatter": _fmt_y, "color": "#8B97A8", "fontSize": 11},
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
            "axisLabel": {"formatter": _fmt_y, "color": "#8B97A8", "fontSize": 11},
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
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def schema(self) -> dict: ...

    @abstractmethod
    async def execute(self, **kwargs) -> str: ...


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
        from sqlalchemy import select
        from app.models.datasource import DataSource, SourceType

        if db_session is None:
            return json.dumps({"error": "数据库会话不可用"}, ensure_ascii=False)

        result = await db_session.execute(
            select(DataSource).where(DataSource.user_id == user_id)
        )
        datasources = list(result.scalars().all())

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
            schema_name = duckdb_client.get_schema_name(user_id, str(ds.id), ds.name)
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
        # L3 安全检查
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

        schema_name = duckdb_client.get_schema_name(user_id, datasource_id, datasource.name)

        # 外部数据源 ATTACH
        if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
            from app.utils.crypto import decrypt_value, get_encryption_key
            try:
                duckdb_client.execute(f'DETACH "{schema_name}"')
            except Exception:
                pass
            conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
            key = get_encryption_key()
            if key and conn_info.get("password"):
                conn_info["password"] = decrypt_value(conn_info["password"], key)
            conn_info["user"] = conn_info.get("username", "postgres")
            conn_info["database"] = conn_info.get("db_name", "")
            from app.connectors.postgres_connector import postgres_connector as pg_conn
            attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
            duckdb_client.execute(attach_sql)

        # 执行查询
        try:
            rows_raw = duckdb_client.fetchall(final_sql)
            cols = []
            select_match = re.match(
                r"SELECT\s+(.+?)\s+FROM", final_sql,
                re.IGNORECASE | re.DOTALL,
            )
            if select_match:
                cols_str = select_match.group(1)
                col_parts = re.split(r",(?![^(]*\))", cols_str)
                for part in col_parts:
                    part = part.strip()
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
            if not cols and rows_raw:
                cols = [f"col_{i}" for i in range(len(rows_raw[0]))]

            def _safe(v):
                if v is None:
                    return None
                if isinstance(v, (int, float, str, bool)):
                    return v
                return str(v)

            data_rows = [[_safe(v) for v in row] for row in rows_raw[:50]]
            return json.dumps({
                "columns": cols,
                "row_count": len(data_rows),
                "rows": data_rows,
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
        if not rows or not columns:
            return json.dumps({"error": "数据为空"}, ensure_ascii=False)

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

        return json.dumps({
            "chart_type": chart_type,
            "option": option,
        }, ensure_ascii=False)


# ==================== 注册工具 ====================

list_datasources_tool = ListDatasourcesTool()
query_datasource_tool = QueryDatasourceTool()
render_chart_tool = RenderChartTool()

ToolRegistry.register(list_datasources_tool)
ToolRegistry.register(query_datasource_tool)
ToolRegistry.register(render_chart_tool)
