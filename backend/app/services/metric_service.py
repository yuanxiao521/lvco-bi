"""指标语义层服务：指标定义 CRUD + 指标解析执行（resolve_metric）。

指标是"一次定义、处处引用"的一等公民：
- CRUD 提供指标中心管理（新增/编辑口径）
- resolve_metric 把「指标 + 维度 + 过滤」解析成可执行查询配置，
  画布块/图表/AI 通过 metric_id 引用，保证口径统一、可随口径更新。
"""
import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metric import MetricDefinition
from app.schemas.query import ChartQueryConfig, FilterConfig, MeasureConfig

logger = logging.getLogger("lvco.metric_service")


# 内置通用模板指标（user_id=None 表示全局公开模板，可绑定到同结构数据源）
DEFAULT_TEMPLATE_METRICS: list[dict] = [
    {
        "key": "sales_amount",
        "name": "销售额",
        "description": "成交金额合计（含税口径）",
        "formula": "SUM({{amount}})",
        "agg_kind": "SUM",
    },
    {
        "key": "order_count",
        "name": "订单量",
        "description": "订单总笔数（按订单号去重）",
        "formula": "COUNT({{order_id}})",
        "agg_kind": "COUNT",
    },
    {
        "key": "customer_count",
        "name": "客户数",
        "description": "去重客户总数",
        "formula": "COUNT(DISTINCT {{customer_id}})",
        "agg_kind": "COUNT_DISTINCT",
    },
    {
        "key": "avg_price",
        "name": "客单价",
        "description": "平均单笔金额（销售额/订单量）",
        "formula": "AVG({{amount}})",
        "agg_kind": "AVG",
    },
]


class MetricServiceError(Exception):
    """指标语义层业务异常。"""


def extract_metric_fields(formula: str) -> list[str]:
    """从 formula 中提取模板占位字段（{{field}}），去重保留出现顺序。

    - SUM({{amount}})  -> ["amount"]（动态字段）
    - SUM("amount") / COUNT(*) -> []（字段已写死在 formula 内）
    """
    seen: dict[str, None] = {}
    for m in re.finditer(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", formula or ""):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def assert_formula_allowed(formula: str) -> None:
    """校验 formula 只含白名单聚合，防注入/任意子查询。"""
    upper = (formula or "").upper().strip()
    if not upper:
        raise MetricServiceError("指标 formula 不能为空")
    if ";" in upper or "--" in upper or "/*" in upper or "*/" in upper:
        raise MetricServiceError("指标 formula 含不允许的片段")
    # 允许的单一聚合形状：SUM(x) / COUNT(x) / COUNT(DISTINCT x)，
    # 其中 x 可以是 {{模板占位}}、"内联字段"、裸标识符或 *。
    pattern = (
        r"^(SUM|AVG|MAX|MIN|STDDEV|MEDIAN|COUNT)"
        r"\s*\(\s*(?:DISTINCT\s+)?("
        r"\{\{[^{}]+\}\}"          # {{占位}}
        r'|"[^"]*"'                # "内联字段"
        r"|[A-Za-z_][A-Za-z0-9_]*"  # 裸标识符
        r"|\*"                      # COUNT(*)
        r")\s*\)\s*$"
    )
    if not re.match(pattern, upper, re.IGNORECASE):
        raise MetricServiceError(
            "指标 formula 结构不支持：仅允许单一聚合，如 SUM(\"amount\") / COUNT(DISTINCT \"customer_id\")"
        )


async def get_metric_by_key(db: AsyncSession, key: str, user_id: UUID | None) -> MetricDefinition | None:
    """按 key 查指标（优先用户私有，其次全局/公开模板）。"""
    stmt = select(MetricDefinition).where(
        MetricDefinition.key == key,
        MetricDefinition.active.is_(True),
    )
    if user_id is not None:
        stmt = stmt.order_by(
            (MetricDefinition.user_id == user_id).desc(),
        )
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def get_metric(db: AsyncSession, metric_id: UUID, user_id: UUID | None) -> MetricDefinition:
    """按 id 查指标；不存在（或对用户不可见）时抛 MetricServiceError。"""
    stmt = select(MetricDefinition).where(MetricDefinition.id == metric_id)
    if user_id is not None:
        stmt = stmt.where(
            (MetricDefinition.user_id == user_id) | (MetricDefinition.user_id.is_(None))
        )
    result = await db.execute(stmt)
    metric = result.scalar_one_or_none()
    if metric is None:
        raise MetricServiceError(f"指标不存在或无权访问: {metric_id}")
    return metric


async def list_metrics_for_user(db: AsyncSession, user_id: UUID | None) -> list[MetricDefinition]:
    """列出当前用户可引用的全部指标：用户私有 + 全局模板（user_id IS NULL）。

    用于 AI 对话上下文注入，让 Planner 优先引用命名指标而非裸字段。
    """
    stmt = (
        select(MetricDefinition)
        .where(MetricDefinition.active.is_(True))
        .order_by(MetricDefinition.name.asc())
    )
    if user_id is not None:
        stmt = stmt.where(
            (MetricDefinition.user_id == user_id) | (MetricDefinition.user_id.is_(None))
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def format_metrics_context(metrics: list[MetricDefinition]) -> str:
    """把指标清单格式化为 AI 可读的上下文文本。

    沿用"口径优先"原则：每个指标给出 key（供工具引用）+ 名称 + 口径说明 + 聚合表达式，
    避免 LLM 直接臆造列名或口径。
    """
    if not metrics:
        return ""
    lines = ["可用指标（建议优先用指标 key 而非裸字段聚合）："]
    for m in metrics:
        desc = f"，{m.description}" if m.description else ""
        lines.append(f"- key={m.key}｜{m.name}{desc}（表达式: {m.formula}）")
    return "可引用指标清单：\n" + "\n".join(lines)


def resolve_to_expression(metric: MetricDefinition, dimensions: list[str]) -> str:
    """将指标的 formula 解析为最终 SQL 聚合表达式。

    - 模板 formula（含 {{field}}）：用第一个可用维度/度量字段替换占位符。
    - 常量 formula（字段已写死）：原样返回。
    """
    formula = metric.formula
    placeholders = extract_metric_fields(formula)
    if not placeholders:
        assert_formula_allowed(formula)
        return formula.strip()
    if not dimensions:
        raise MetricServiceError(
            f"指标 '{metric.key}' 是模板指标（formula={formula}），需先指定字段才能解析"
        )
    resolved = formula
    for i, token in enumerate(placeholders):
        # 占位符由动态度量字段填充；若维度给的是受信 SQL 片段则直接用
        src = dimensions[i] if i < len(dimensions) else token
        resolved = resolved.replace(f"{{{{{token}}}}}", f'"{src}"')
    assert_formula_allowed(resolved)
    return resolved.strip()


def resolve_metric(
    metric: MetricDefinition,
    dimensions: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    chart_type: str | None = None,
    datasource_id: UUID | None = None,
    limit: int = 1000,
) -> ChartQueryConfig:
    """把指标解析成可执行查询配置（供 execute_chart_query 使用）。

    Args:
        metric: 指标定义（含口径 formula / table_ref / agg_kind）。
        dimensions: 维度字段列表（可为空，指标做单值汇总）。
        filters: 过滤条件（[{field, op, value}, ...]）。
        chart_type: 图表类型。
        datasource_id: 指标实际绑定的数据源；为空时由调用方在配置外层指定。
        limit: 返回行数上限。

    Returns:
        ChartQueryConfig：以"表达式度量"（expression）形式承载指标口径。
    """
    expression = resolve_to_expression(metric, [str(d) for d in (dimensions or [])])
    agg = (metric.agg_kind or "SUM").upper()
    alias = f"{agg.lower()}_{metric.key}" if agg else metric.key
    return ChartQueryConfig(
        dimensions=[str(d) for d in (dimensions or [])],
        measures=[MeasureConfig(field="", agg=agg, expression=expression, alias=alias)],
        filters=[FilterConfig(**f) for f in (filters or [])],
        chart_type=chart_type,
        datasource_id=str(datasource_id) if datasource_id else None,
        limit=limit,
    )


async def ensure_default_metrics(db: AsyncSession) -> None:
    """幂等地写入内置全局模板指标（user_id=None）。仅当 key 缺失时插入。"""
    for spec in DEFAULT_TEMPLATE_METRICS:
        existing = await db.execute(
            select(MetricDefinition).where(
                MetricDefinition.key == spec["key"],
                MetricDefinition.user_id.is_(None),
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue
        db.add(MetricDefinition(user_id=None, **spec))
    await db.flush()


def measure_to_metric_ref(measure: dict) -> str | None:
    """从 measure 字典中提取 metric 引用标识（metric_id 或 metric_key），无则 None。"""
    if not isinstance(measure, dict):
        return None
    return measure.get("metric_id") or measure.get("metricKey") or measure.get("metric_key")


async def resolve_measures(
    db: AsyncSession,
    user_id: UUID | None,
    raw_measures: list[dict] | None,
    dimensions: list[str] | None = None,
) -> tuple[list[MeasureConfig], list[dict]]:
    """把画布块的原始 measures 解析为可执行度量 + 前端展示度量。

    - 普通度量：{field, agg} 直接透传。
    - 指标度量：{metric_id 或 metric_key} + 可选 dimensions 覆盖 → 解析为表达式度量，
      同时返回带 metric 引用的展示形态（前端据此随口径刷新）。

    Returns:
        (executable_measures, display_measures)
        executable_measures 喂给 query_engine，display_measures 写入画布块。
    """
    executable: list[MeasureConfig] = []
    display: list[dict] = []
    for m in raw_measures or []:
        if not isinstance(m, dict):
            continue
        ref = measure_to_metric_ref(m)
        if ref:
            # 优先按 id，其次按 key（key 会做可见性过滤）
            metric = None
            try:
                metric = await get_metric(db, UUID(str(ref)), user_id)
            except (MetricServiceError, ValueError):
                metric = await get_metric_by_key(db, str(ref), user_id)
            if metric is None:
                raise MetricServiceError(f"指标不存在或无权访问: {ref}")
            cfg = resolve_metric(
                metric,
                # 模板指标（含 {{field}}）优先用度量自带的 field 填充占位字段；
                # 未提供时回退用维度分组字段填充，保证不落空。
                dimensions=[str(m["field"])] if m.get("field") else (dimensions or []),
                chart_type=None,
            )
            executable.append(cfg.measures[0])
            display.append({
                "metric_id": str(metric.id),
                "metric_key": metric.key,
                "metric_name": metric.name,
                "expression": cfg.measures[0].expression,
            })
        else:
            field = m.get("field")
            if not field:
                continue
            agg = (m.get("agg") or "SUM").upper()
            executable.append(MeasureConfig(field=str(field), agg=agg))
            display.append({"field": str(field), "agg": agg})
    return executable, display

async def resolve_measures_for_exec(db: AsyncSession, user_id: UUID | None,
                                    raw_measures: list[dict] | None,
                                    dimensions: list[str] | None = None) -> list[MeasureConfig]:
    """仅解析可执行度量（不关心展示层），供查询链路复用。"""
    executable, _ = await resolve_measures(db, user_id, raw_measures, dimensions)
    return executable