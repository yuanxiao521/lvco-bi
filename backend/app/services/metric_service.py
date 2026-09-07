"""指标语义层服务：指标定义 CRUD + 指标解析执行（resolve_metric）。

指标是"一次定义、处处引用"的一等公民：
- CRUD 提供指标中心管理（新增/编辑口径）
- resolve_metric 把「指标 + 维度 + 过滤」解析成可执行查询配置，
  画布块/图表/AI 通过 metric_id 引用，保证口径统一、可随口径更新。
"""
import logging
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.datasource import DataSource
from app.models.metric import MetricDefinition
from app.models.metric_lineage import MetricLineage
from app.models.metric_usage import MetricUsage
from app.models.metric_version import MetricVersion
from app.repositories.unit_of_work import UnitOfWork
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


_ALLOWED_AGGS = {"SUM", "AVG", "MAX", "MIN", "STDDEV", "MEDIAN", "COUNT"}


def _validate_derived_expression(expr: str) -> bool:
    """校验派生指标表达式是否结构合法。

    只允许：白名单聚合调用、metric("key") 引用、带引号字段、数字，以及四则运算。
    裸标识符/SQL 关键字（如 SELECT、FROM）不得作为独立操作数出现，
    因此子查询类公式（如 ``(SELECT 1)``）会被拒绝。
    """
    _AGG_ARG = (
        r'(?:"[^"]*")'                       # "字段名"
        r'|(?:\*)'                            # COUNT(*)
        r"|(?:[A-Za-z_][A-Za-z0-9_.]*)"        # 裸字段名/限定名
        r"|(?:[0-9]+(?:\.[0-9]+)?)"            # 数字
    )

    i, n = 0, len(expr or "")
    depth = 0
    prev_operand = False

    def skip_ws() -> None:
        nonlocal i
        while i < n and expr[i].isspace():
            i += 1

    skip_ws()
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            prev_operand = False
            skip_ws()
            continue
        if ch == ")":
            depth -= 1
            if depth < 0:
                return False
            i += 1
            prev_operand = True
            skip_ws()
            continue
        if ch in "+-*/":
            if not prev_operand:  # 二元运算符前必须已有运算数
                return False
            i += 1
            prev_operand = False
            skip_ws()
            continue
        m = re.match(r"[0-9]+(?:\.[0-9]+)?", expr[i:])  # 数字
        if m:
            i += m.end()
            prev_operand = True
            skip_ws()
            continue
        m = re.match(r'metric\s*\(\s*[\'"][^\'"]+[\'"]\s*\)', expr[i:], re.IGNORECASE)
        if m:
            i += m.end()
            prev_operand = True
            skip_ws()
            continue
        m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', expr[i:])  # 白名单聚合调用
        if m and m.group(1).upper() in _ALLOWED_AGGS:
            i = expr.find(")", m.end())
            if i == -1:
                return False
            arg = expr[m.end():i].strip()
            if arg.upper().startswith("DISTINCT"):
                bits = arg.split(None, 1)
                arg = bits[1].strip() if len(bits) == 2 else ""
            # 聚合参数须为单个标量 token（或 DISTINCT + 该 token）
            if not re.fullmatch(_AGG_ARG, arg):
                return False
            i += 1  # 跳过 ')'
            prev_operand = True
            skip_ws()
            continue
        m = re.match(r'"[^"]*"', expr[i:])  # 带引号字段
        if m:
            i += m.end()
            prev_operand = True
            skip_ws()
            continue
        return False  # 独立裸标识符 / 未知符号 → 拒绝（防 SELECT/FROM/子查询）
    return depth == 0 and prev_operand


def assert_formula_allowed(formula: str) -> None:
    """校验 formula 只含白名单聚合或派生表达式，防注入/任意子查询。"""
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
    # 单一聚合直接放行；否则要求是合法的派生表达式（含 metric("key") 引用）
    if not re.match(pattern, upper, re.IGNORECASE) and not _validate_derived_expression(formula):
        raise MetricServiceError(
            "指标 formula 结构不支持：仅允许单一聚合（如 SUM(\"amount\")）"
            "或派生表达式（如 metric(\"base_key\") * 1.2）"
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


async def record_metric_usage(db: AsyncSession, metric_id: UUID, usage_type: str, usage_ref_id: str | None, user_id: UUID | None) -> None:
    if usage_ref_id is not None:
        stmt = select(MetricUsage).where(
            MetricUsage.metric_id == metric_id,
            MetricUsage.usage_type == usage_type,
            MetricUsage.usage_ref_id == usage_ref_id,
            MetricUsage.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        if result.scalar_one_or_none() is not None:
            return
        usage = MetricUsage(
            metric_id=metric_id,
            usage_type=usage_type,
            usage_ref_id=usage_ref_id,
            user_id=user_id,
        )
        db.add(usage)
    else:
        stmt = select(MetricUsage).where(
            MetricUsage.metric_id == metric_id,
            MetricUsage.usage_type == usage_type,
            MetricUsage.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        for usage in result.scalars().all():
            usage.deleted_at = datetime.now(timezone.utc)
    await db.flush()


def extract_metric_ids_from_measures(measures: list) -> list[str]:
    result: list[str] = []
    for m in measures or []:
        if isinstance(m, dict):
            mid = m.get("metric_id")
            if mid:
                result.append(str(mid))
    return result


def extract_metric_ids_from_blocks(blocks: list) -> list[str]:
    seen: set[str] = set()
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "chart":
            qc = block.get("queryConfig") or {}
            for mid in extract_metric_ids_from_measures(qc.get("measures", [])):
                seen.add(mid)
    return list(seen)


class MetricService:
    """指标治理服务：扩展方法，依赖 UnitOfWork 访问持久层。"""

    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def record_usage(self, metric_id: UUID, usage_type: str, usage_ref_id: str | None, user_id: UUID | None) -> None:
        if usage_ref_id is not None:
            stmt = select(MetricUsage).where(
                MetricUsage.metric_id == metric_id,
                MetricUsage.usage_type == usage_type,
                MetricUsage.usage_ref_id == usage_ref_id,
                MetricUsage.deleted_at.is_(None),
            )
            row = await self.uow.db.execute(stmt)
            if row.scalar_one_or_none() is not None:
                return
            usage = MetricUsage(
                metric_id=metric_id,
                usage_type=usage_type,
                usage_ref_id=usage_ref_id,
                user_id=user_id,
            )
            self.uow.db.add(usage)
        else:
            stmt = select(MetricUsage).where(
                MetricUsage.metric_id == metric_id,
                MetricUsage.usage_type == usage_type,
                MetricUsage.deleted_at.is_(None),
            )
            rows = await self.uow.db.execute(stmt)
            for usage in rows.scalars().all():
                usage.deleted_at = datetime.now(timezone.utc)
        await self.uow.flush()

    async def resolve_dependencies(self, metric_id: UUID) -> list[str]:
        """递归解析依赖的 metric_id 列表（DAG 广度优先去重）。"""
        result: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque()
        queue.append(str(metric_id))

        while queue:
            current_id = queue.popleft()
            stmt = select(MetricDefinition).where(MetricDefinition.id == current_id)
            row = await self.uow.db.execute(stmt)
            metric = row.scalar_one_or_none()
            if metric is None:
                continue
            for dep_id in (metric.depends_on_metric_ids or []):
                if dep_id not in seen:
                    seen.add(dep_id)
                    result.append(dep_id)
                    queue.append(dep_id)
        return result

    async def get_dependents(self, metric_id: UUID) -> list[MetricUsage]:
        """反向查询谁引用了该指标（未软删除的引用）。"""
        stmt = (
            select(MetricUsage)
            .where(
                MetricUsage.metric_id == metric_id,
                MetricUsage.deleted_at.is_(None),
            )
        )
        row = await self.uow.db.execute(stmt)
        return list(row.scalars().all())

    async def impact_analysis(self, metric_id: UUID) -> dict:
        """影响报告：{metric_id, name, dependents_count, dashboards_count, users_count}。"""
        metric_stmt = select(MetricDefinition).where(MetricDefinition.id == metric_id)
        row = await self.uow.db.execute(metric_stmt)
        metric = row.scalar_one_or_none()
        if metric is None:
            raise MetricServiceError(f"指标不存在: {metric_id}")

        dependents = await self.get_dependents(metric_id)
        # 派生指标通过 depends_on_metric_ids 引用本指标（口径与 /dependents 一致）
        referrers = await self.uow.db.execute(
            select(MetricDefinition).where(MetricDefinition.active.is_(True))
        )
        referrer_count = sum(
            1 for m in referrers.scalars().all()
            if str(metric_id) in (m.depends_on_metric_ids or [])
        )
        dashboards: set[str] = set()
        users: set[str] = set()
        for dep in dependents:
            if dep.usage_type == "dashboard" and dep.usage_ref_id:
                dashboards.add(dep.usage_ref_id)
            if dep.user_id:
                users.add(str(dep.user_id))

        return {
            "metric_id": str(metric_id),
            "name": metric.name,
            "dependents_count": sum(1 for _ in dependents) + referrer_count,
            "dashboards_count": len(dashboards),
            "users_count": len(users),
        }

    async def _extract_lineage(self, metric_id: UUID, formula: str) -> None:
        await self.uow.db.execute(
            delete(MetricLineage).where(MetricLineage.metric_id == metric_id)
        )
        if not formula or not formula.strip():
            await self.uow.flush()
            return

        result = await self.uow.db.execute(
            select(MetricDefinition).where(MetricDefinition.id == metric_id)
        )
        metric = result.scalar_one_or_none()
        if metric is None:
            return

        seen = set()
        records = []

        for m in re.finditer(r"(\w+)\s*\(\s*([^)]+)\s*\)", formula):
            transform = m.group(1).upper()
            field_expr = m.group(2).strip()

            if transform == "METRIC":
                inner = re.search(r"['\"]([^'\"]+)['\"]", field_expr)
                if inner:
                    field_name = inner.group(1)
                    key = (field_name, "metric_ref")
                    if key not in seen:
                        seen.add(key)
                        records.append(MetricLineage(
                            metric_id=metric_id,
                            source_field=field_name,
                            transform="metric_ref",
                            target_field=field_name,
                            dataset_id=metric.datasource_id,
                        ))
                continue

            if field_expr.upper().startswith("DISTINCT "):
                field_expr = field_expr[9:].strip()

            tmpl = re.search(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", field_expr)
            if tmpl:
                field_name = tmpl.group(1)
            else:
                quoted = re.search(r'"([^"]*)"', field_expr)
                field_name = quoted.group(1) if quoted else field_expr

            key = (field_name, transform)
            if key not in seen:
                seen.add(key)
                records.append(MetricLineage(
                    metric_id=metric_id,
                    source_field=field_name,
                    transform=transform,
                    target_field=field_name,
                    dataset_id=metric.datasource_id,
                ))

        from app.services.metric_parser import parse_derived_formula
        for ref in parse_derived_formula(formula):
            key = (ref.metric_key, "metric_ref")
            if key not in seen:
                seen.add(key)
                records.append(MetricLineage(
                    metric_id=metric_id,
                    source_field=ref.metric_key,
                    transform="metric_ref",
                    target_field=ref.metric_key,
                    dataset_id=metric.datasource_id,
                ))

        for rec in records:
            self.uow.db.add(rec)
        await self.uow.flush()

    async def check_schema_impact(self, dataset_id: UUID) -> list[dict]:
        result = await self.uow.db.execute(
            select(MetricLineage.metric_id)
            .where(MetricLineage.dataset_id == dataset_id)
            .distinct()
        )
        metric_ids = [row[0] for row in result.all()]
        if not metric_ids:
            return []

        metrics_result = await self.uow.db.execute(
            select(MetricDefinition).where(MetricDefinition.id.in_(metric_ids))
        )
        metrics = list(metrics_result.scalars().all())
        return [
            {
                "metric_id": str(m.id),
                "metric_key": m.key,
                "metric_name": m.name,
                "formula": m.formula,
            }
            for m in metrics
        ]

    async def get_lineage(self, metric_id: UUID) -> list[dict]:
        """字段血缘链：[{source_field, transform, target_field, dataset_name}]。"""
        stmt = (
            select(MetricLineage, DataSource.name)
            .outerjoin(DataSource, MetricLineage.dataset_id == DataSource.id)
            .where(MetricLineage.metric_id == metric_id)
        )
        row = await self.uow.db.execute(stmt)
        result: list[dict] = []
        for lineage, ds_name in row.all():
            result.append({
                "source_field": lineage.source_field,
                "transform": lineage.transform,
                "target_field": lineage.target_field,
                "dataset_name": ds_name,
            })
        return result

    async def publish_version(self, metric_id: UUID, change_note: str, user_id: UUID) -> MetricVersion:
        """创建版本快照：读取当前 MetricDefinition 全部字段，序列化为 JSON 快照，version + 1。"""
        stmt = select(MetricDefinition).where(MetricDefinition.id == metric_id)
        row = await self.uow.db.execute(stmt)
        metric = row.scalar_one_or_none()
        if metric is None:
            raise MetricServiceError(f"指标不存在: {metric_id}")

        new_version = metric.version + 1
        snapshot: dict[str, Any] = {
            "key": metric.key,
            "name": metric.name,
            "description": metric.description,
            "formula": metric.formula,
            "agg_kind": metric.agg_kind,
            "datasource_id": str(metric.datasource_id) if metric.datasource_id else None,
            "table_ref": metric.table_ref,
            "active": metric.active,
            "formula_type": metric.formula_type,
            "depends_on_metric_ids": metric.depends_on_metric_ids or [],
        }

        version = MetricVersion(
            metric_id=metric_id,
            version=new_version,
            snapshot=snapshot,
            change_note=change_note,
            created_by=user_id,
        )
        self.uow.db.add(version)
        metric.version = new_version
        await self.uow.flush()
        return version

    async def rollback_to(self, metric_id: UUID, version: int, user_id: UUID) -> MetricDefinition:
        """回滚到历史版本：从 metric_versions 读取快照，恢复字段，version + 1（新版本）。"""
        ver_stmt = select(MetricVersion).where(
            MetricVersion.metric_id == metric_id,
            MetricVersion.version == version,
        )
        row = await self.uow.db.execute(ver_stmt)
        ver = row.scalar_one_or_none()
        if ver is None:
            raise MetricServiceError(f"版本记录不存在: metric_id={metric_id}, version={version}")

        snapshot = ver.snapshot
        metric_stmt = select(MetricDefinition).where(MetricDefinition.id == metric_id)
        row = await self.uow.db.execute(metric_stmt)
        metric = row.scalar_one_or_none()
        if metric is None:
            raise MetricServiceError(f"指标不存在: {metric_id}")

        new_version = metric.version + 1
        metric.key = snapshot["key"]
        metric.name = snapshot["name"]
        metric.description = snapshot.get("description")
        metric.formula = snapshot["formula"]
        metric.agg_kind = snapshot.get("agg_kind")
        metric.datasource_id = UUID(snapshot["datasource_id"]) if snapshot.get("datasource_id") else None
        metric.table_ref = snapshot.get("table_ref")
        metric.active = snapshot.get("active", True)
        metric.formula_type = snapshot.get("formula_type", "basic")
        metric.depends_on_metric_ids = snapshot.get("depends_on_metric_ids", [])
        metric.version = new_version

        rollback_snapshot = {**snapshot}
        new_ver = MetricVersion(
            metric_id=metric_id,
            version=new_version,
            snapshot=rollback_snapshot,
            change_note=f"Rollback to version {version}",
            created_by=user_id,
        )
        self.uow.db.add(new_ver)
        await self.uow.flush()
        await self.uow.db.refresh(metric)  # 补齐 updated_at 等数据库端字段，避免懒加载越界
        return metric

    async def compare_versions(self, metric_id: UUID, from_version: int, to_version: int) -> dict:
        """版本对比：读取两个版本快照，返回字段级 diff。"""
        stmt = select(MetricVersion).where(
            MetricVersion.metric_id == metric_id,
            MetricVersion.version.in_([from_version, to_version]),
        )
        row = await self.uow.db.execute(stmt)
        versions: dict[int, MetricVersion] = {v.version: v for v in row.scalars().all()}

        v_from = versions.get(from_version)
        v_to = versions.get(to_version)
        if v_from is None:
            raise MetricServiceError(f"版本 {from_version} 不存在")
        if v_to is None:
            raise MetricServiceError(f"版本 {to_version} 不存在")

        diff_fields: list[dict[str, Any]] = []
        all_keys = set(list(v_from.snapshot.keys()) + list(v_to.snapshot.keys()))
        for key in sorted(all_keys):
            old_val = v_from.snapshot.get(key)
            new_val = v_to.snapshot.get(key)
            if old_val != new_val:
                diff_fields.append({
                    "field": key,
                    "from": old_val,
                    "to": new_val,
                })

        return {
            "metric_id": str(metric_id),
            "from_version": from_version,
            "to_version": to_version,
            "changed": len(diff_fields) > 0,
            "diff_fields": diff_fields,
        }