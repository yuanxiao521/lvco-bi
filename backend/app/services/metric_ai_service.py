# -*- coding: utf-8 -*-
"""指标中心「AI 起草」：一句提示词 → 指标草稿（**不落库**）。

产品定位
--------
用户在指标中心输入「已支付订单的销售额」，LLM 产出草稿（key / 名称 / 口径说明 /
formula / 歧义提示），后端做**确定性校验**后返回，前端展示给人确认；人确认后再调
已有的 POST /metrics 落库。所以本模块：

- **绝不写库**（没有 db.add / commit，也不产生任何草稿表）；
- **不注册任何 Lead/agent 工具**（用户明确：暂时只给人用，不给 agent 调）；
- 定位是"起草助手"，不是"自动建指标"：有歧义时**写进 ambiguities 让人定**，
  而不是猜一个口径就落库（口径错了比没有更贵）。

为什么后端还要再校验一遍（模型会说大话）
----------------------------------------
1. 公式：复用 formula_guard 的 AST 白名单（与 POST /metrics 同一条边界）；
2. 字段：公式里引用的列必须真的在该数据源 schema 里（防"金额"→amount 这类幻觉）；
3. 类型：SUM/AVG/STDDEV/MEDIAN 只能作用在数值列（否则试算必炸，且落库后每次查询都炸）；
4. key：格式合法且不与用户可见的既有指标冲突（**不自动改名**——让人决定）。
校验失败也返回 200（带 validation.errors）：这是"草稿没写好"，不是"接口出错"，
前端要把错误展示给用户改 prompt，而不是弹一个红色报错。

试算（best-effort）
-------------------
草稿校验通过后，用与正式查询**同一条链路**（resolve_metric → execute_chart_query）
跑一次 LIMIT 5，把第一个数值作为 sampleValue；失败只写 sampleError，不影响草稿返回。
这样用户看到的是一个"能跑出数"的草稿，而不是一段看起来对的 SQL。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID, uuid4

import sqlglot
from sqlglot import exp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.datasource import DataSource
from app.models.metric import MetricDefinition
from app.services.formula_guard import (
    ALLOWED_AGG_SQL_NAMES,
    FormulaRejected,
    validate_formula,
)
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient
from app.services.metric_service import MetricServiceError, resolve_metric
from app.services.profile_schema import norm_type, type_kind
from app.services.query_engine import execute_chart_query

logger = logging.getLogger("lvco.metric_ai")

# ── 上下文预算（省 token 且保准确）─────────────────────────────────────────
MAX_CONTEXT_FIELDS = 40          # 字段最多给 40 个（宽表只给前 40，避免 prompt 爆炸）
MAX_EXISTING_METRICS = 30        # 既有指标最多 30 条（防 key 冲突 / 支持 metric() 引用）
SAMPLE_LIMIT = 5                 # 试算只取 5 行

# key 规则：小写字母开头，只含小写字母/数字/下划线，2-61 字符
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
# 只能用数值列的聚合（需求明确）
NUMERIC_ONLY_AGGS = frozenset({"SUM", "AVG", "STDDEV", "MEDIAN"})
# 可以写在草稿 aggKind 里的聚合（与 MetricCreate 的 AggKind 对齐）
DRAFT_AGG_KINDS = frozenset({
    "SUM", "AVG", "COUNT", "MAX", "MIN", "MEDIAN", "STDDEV",
})

_SYSTEM_PROMPT = """你是「Lvco BI 指标中心」的指标起草助手。用户用一句中文描述业务指标，你产出一个可直接落库的指标定义草稿。

## 指标是什么（能力边界）
指标 = **一个聚合表达式**。formula 只能是：
- 聚合：SUM / AVG / COUNT / COUNT(DISTINCT …) / MAX / MIN / MEDIAN / STDDEV
- 四则运算与括号、CASE WHEN、COALESCE / NULLIF、CAST / TRY_CAST
- 引用**已有指标**：metric("已有指标的key")，例如 metric("sales_amount") * 1.2

严格禁止（写了会被后端拒绝）：
- 子查询 / SELECT / WITH / UNION、JOIN、FROM（指标是表达式，不是查询）
- 窗口函数 OVER(...)
- 多条语句（分号）、SQL 注释（-- 和 /* */）
- 不存在的函数、编造的字段名

## 字段规范
- 字段名必须**逐字复制**上下文【可用字段】里的列名，并用双引号包裹："amount"
- 中文名（如 金额）只能出现在 name/description 里，不允许出现在 formula 里
- 时间范围不要写进 formula（指标口径不含时间过滤，时间由查询层控制）

## 口径（description）必须写清
1-3 句，必须覆盖：**是否含税、是否去重、统计范围（全量/已支付/已完成）、是否排除退款**等口径要点。
例：成交金额合计，取已支付订单的 amount 求和，含税；不含退款订单。

## 歧义必须说出来（不许猜了不吭声）
如果这句话存在多种合理口径（是否含税、是否去重订单、是否含未支付/退款、口径字段不同），
**选最常规的一种写进 formula**，其余分歧逐条写进 ambiguities 数组（每条一句话，指明分歧点与可选方案）。
没有歧义就给空数组。

## 输出格式（只输出 JSON 对象，不要 markdown 代码块，不要任何解释文字）
{
  "key": "小写字母开头，只含小写字母/数字/下划线，例如 paid_order_amount",
  "name": "中文指标名，简短，例如 已支付订单销售额",
  "description": "口径说明（见上）",
  "formula": "聚合表达式，例如 SUM(CASE WHEN "status" = 'paid' THEN "amount" ELSE 0 END)",
  "agg_kind": "SUM/AVG/COUNT/MAX/MIN/MEDIAN/STDDEV 之一",
  "ambiguities": ["（可选）口径分歧点"]
}
"""


# ── 异常（路由层映射到 HTTP）──────────────────────────────────────────────

class MetricDraftError(Exception):
    """指标起草的领域错误（路由转成 HTTPException）。"""

    status_code = 400
    code = "INVALID_METRIC_DRAFT"

    def __init__(self, message: str) -> None:
        self.message = str(message)
        super().__init__(self.message)


class MetricDraftUnavailable(MetricDraftError):
    """AI 未配置 / 上游不可用 → 503。"""

    status_code = 503
    code = "AI_UNAVAILABLE"


class MetricDraftDatasourceNotFound(MetricDraftError):
    """数据源不存在或不属于当前用户 → 404。"""

    status_code = 404
    code = "DATASOURCE_NOT_FOUND"


# ── 上下文装配 ────────────────────────────────────────────────────────────

def _field_sensitive(field: dict) -> bool:
    """字段是否敏感：优选手工标记，其次按列名启发式（与 schema 同步口径一致）。"""
    if "sensitive" in field:
        return bool(field.get("sensitive"))
    from app.services.sensitive_columns import detect_sensitive

    return bool(detect_sensitive(str(field.get("name") or "")))


def _render_field_line(field: dict) -> str:
    name = str(field.get("name") or "").strip()
    dtype = norm_type(field.get("data_type") or field.get("type"))
    kind = type_kind(dtype)
    category = str(field.get("category") or "").strip()
    parts = [f'- "{name}" ({dtype or "UNKNOWN"}, {kind})']
    if category:
        parts.append(f"[{category}]")
    samples = field.get("sample") or []
    if _field_sensitive(field):
        parts.append("（敏感列，无样例）")
    elif isinstance(samples, (list, tuple)) and samples:
        rendered = ", ".join(str(s) for s in list(samples)[:3])
        parts.append(f"样例: {rendered}")
    return " ".join(parts)


def _render_schema_block(ds: DataSource, fields: list[dict]) -> str:
    """数据源 + 字段清单（最多 40 个）。"""
    stype = str(getattr(getattr(ds, "source_type", ""), "value", getattr(ds, "source_type", "")) or "?")
    lines = [f"【数据源】{getattr(ds, 'name', '') or '未命名'}（{stype}）"]
    shown = [f for f in fields if isinstance(f, dict) and f.get("name")][:MAX_CONTEXT_FIELDS]
    lines.append(f"【可用字段】（共 {len(fields)} 个，此处展示 {len(shown)} 个；格式：列名(类型, 类型类)）")
    lines.extend(_render_field_line(f) for f in shown)
    if len(fields) > len(shown):
        lines.append(f"…（其余 {len(fields) - len(shown)} 个字段未展示，如需请让用户缩小范围）")
    if not shown:
        lines.append("-（该数据源尚未同步字段）")
    return "\n".join(lines)


def _render_metrics_block(rows: list[Any]) -> str:
    """既有指标清单（key/name/formula），供防冲突与 metric() 引用。"""
    if not rows:
        return "【已有指标】无"
    lines = ["【已有指标】（key 不能重复；可在 formula 里用 metric(\"key\") 引用）"]
    for row in rows[:MAX_EXISTING_METRICS]:
        key = getattr(row, "key", None) or (row[0] if isinstance(row, (tuple, list)) else "")
        name = getattr(row, "name", None) or (row[1] if isinstance(row, (tuple, list)) else "")
        formula = getattr(row, "formula", None) or (row[2] if isinstance(row, (tuple, list)) else "")
        lines.append(f"- {key}（{name}）: {formula}")
    return "\n".join(lines)


def build_draft_messages(*, prompt: str, schema_block: str, metrics_block: str) -> list[dict]:
    """组装起草用的 messages（system 固定；user 三块：schema / 既有指标 / 用户需求）。"""
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"{schema_block}\n\n{metrics_block}\n\n【用户需求】\n{prompt}"},
    ]


# ── JSON 解析与容错 ───────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json_object(raw: str) -> dict:
    """从模型输出里抠 JSON 对象：容忍 markdown 代码块围栏（三个反引号）与前后噪声。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("空响应")
    text = _FENCE_RE.sub("", text).strip()
    if text.startswith("{") and text.endswith("}"):
        candidate = text
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("没有找到 JSON 对象")
        candidate = text[start:end + 1]
    obj = json.loads(candidate)
    if not isinstance(obj, dict):
        raise ValueError("JSON 顶层不是对象")
    return obj


# ── 公式结构分析（确定性，不采信模型自述）─────────────────────────────────

def extract_formula_fields(formula: str) -> list[str]:
    """公式里引用的**字段名**（去掉 metric("key") 之后的双引号标识符），去重保序。"""
    text = re.sub(r"metric\s*\(\s*['\"][^'\"]+['\"]\s*\)", " ", formula or "", flags=re.I)
    seen: dict[str, None] = {}
    for m in re.finditer(r'"([^"]+)"', text):
        seen.setdefault(m.group(1), None)
    return list(seen.keys())


def _func_sql_name(node: exp.Func) -> str:
    """函数名的归一化形式（Anonymous 用 .name，其余用 sql_name）。"""
    if isinstance(node, exp.Anonymous):
        return str(getattr(node, "name", "") or "").upper()
    try:
        return str(node.sql_name() or "").upper()
    except Exception:  # noqa: BLE001
        return ""


def _agg_field_pairs(formula: str) -> list[tuple[str, str]]:
    """[(聚合, 直接参数列)]：用于「SUM/AVG 只能作用于数值列」的类型校验。

    用 AST 而不是正则：SUM(CASE WHEN …) 的直接参数是 CASE 表达式、不是列，
    静态看不出类型 → 跳过（类型错误会由试算暴露 sampleError，而不是在这里误杀）。
    COUNT(DISTINCT x) / SUM("amount") 这类「直接列参数」才会进入校验。
    """
    try:
        tree = sqlglot.parse_one(f"SELECT {formula} AS __agg__", dialect="duckdb")
    except Exception:  # noqa: BLE001 - 解析失败说明公式本身就不合法，交给公式校验报错
        return []
    pairs: list[tuple[str, str]] = []
    for node in tree.find_all(exp.Func):
        name = _func_sql_name(node)
        if name not in ALLOWED_AGG_SQL_NAMES:
            continue
        target = node.this
        if isinstance(target, exp.Distinct):
            cols = [c for c in target.expressions if isinstance(c, exp.Column)]
        elif isinstance(target, exp.Column):
            cols = [target]
        else:
            cols = []
        for col in cols:
            if col.name:
                pairs.append((name, col.name))
    return pairs

def extract_references(formula: str) -> list[dict]:
    """从公式解析 [{field, transform}]：字段被哪个聚合包裹；metric() 引用记为 METRIC。"""
    try:
        tree = sqlglot.parse_one(f"SELECT {formula} AS __ref__", dialect="duckdb")
    except Exception:  # noqa: BLE001 - 解析失败就不给 references（草稿仍会因公式校验报错）
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def nearest_func(node: exp.Expression) -> str:
        """最近的**聚合**（或 metric 引用）。

        注意必须跳过 CASE/IF/COALESCE/CAST 与标量函数：它们是表达式结构，
        不是血缘里的 transform —— 否则 SUM(CASE WHEN …) 里的列会被标成 IF。
        """
        cur = node.parent
        while cur is not None:
            if isinstance(cur, exp.Anonymous) and str(getattr(cur, "name", "")).upper() == "METRIC":
                return "METRIC"
            if isinstance(cur, exp.Func):
                name = _func_sql_name(cur)
                if name in ALLOWED_AGG_SQL_NAMES:
                    return name
            cur = cur.parent
        return ""

    def in_condition(node: exp.Expression) -> bool:
        """列是否位于 CASE/IF 的条件分支（而不是 THEN/ELSE 取值分支）。

        用于把 CASE WHEN "status" = 'paid' THEN "amount" END 里的 status
        标成 FILTER（过滤条件），而不是误导性地标成 SUM。
        """
        cur = node.parent
        while cur is not None:
            if isinstance(cur, exp.If):
                cond = cur.args.get("this")
                probe = node
                while probe is not None and probe is not cur:
                    if probe is cond:
                        return True
                    probe = probe.parent
                return False
            cur = cur.parent
        return False

    for node in tree.find_all(exp.Column):
        field = node.name
        transform = "FILTER" if in_condition(node) else nearest_func(node)
        if not field or (field, transform) in seen:
            continue
        seen.add((field, transform))
        out.append({"field": field, "transform": transform})
    return out


def derive_agg_kind(formula: str, ai_value: str | None) -> str:
    """草稿的 aggKind：优先采信模型给的（且在允许集合内），否则从公式推。"""
    value = str(ai_value or "").strip().upper()
    if value in DRAFT_AGG_KINDS:
        return value
    for agg, _field in _agg_field_pairs(formula):
        if agg in DRAFT_AGG_KINDS:
            return agg
    return "SUM"


# ── 三道校验 ──────────────────────────────────────────────────────────────

def validate_draft(
    draft: dict,
    *,
    field_types: dict[str, str],
    existing_keys: set[str],
) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。校验项：公式 / 字段 / 类型 / key；不含 hint 类文案。"""
    errors: list[str] = []
    warnings: list[str] = []
    formula = str(draft.get("formula") or "").strip()
    lower_map = {k.lower(): v for k, v in field_types.items()}

    # 1) 公式结构（与 POST /metrics 同一条边界）
    if not formula:
        errors.append("缺少 formula（指标必须是一个聚合表达式）")
    else:
        if "{{" in formula or "}}" in formula:
            errors.append("公式里不能有 {{占位符}}：AI 草稿必须写死字段名")
        try:
            validate_formula(formula)
        except FormulaRejected as exc:
            errors.append(f"公式不合法：{exc.reason}")

        # 2) 字段必须存在（防编造列名）
        for name in extract_formula_fields(formula):
            if name not in field_types and name.lower() not in lower_map:
                errors.append(f'公式引用了数据源里不存在的字段："{name}"')

        # 3) 类型：SUM/AVG/STDDEV/MEDIAN 只能用于数值列
        for agg, field in _agg_field_pairs(formula):
            if agg not in NUMERIC_ONLY_AGGS:
                continue
            kind = field_types.get(field) or lower_map.get(field.lower())
            if kind is None:
                continue  # 字段不存在上面已经报过，不重复报
            if kind != "numeric":
                errors.append(f'{agg} 只能用于数值字段，而 "{field}" 的类型是 {kind}')

    # 4) key：格式 + 冲突（不自动改名，让人决定）
    key = str(draft.get("key") or "").strip()
    if not key:
        errors.append("缺少 key（小写字母开头，只含小写字母/数字/下划线）")
    elif not KEY_RE.match(key):
        errors.append("key 不合法：需匹配 ^[a-z][a-z0-9_]{1,60}$（小写字母开头，2-61 个字符）")
    elif key in existing_keys:
        errors.append(f"指标 key 已存在：{key}（请在创建时改一个 key）")

    if not str(draft.get("name") or "").strip():
        errors.append("缺少指标名称 name")
    if not str(draft.get("description") or "").strip():
        warnings.append("建议补充口径说明（是否含税 / 是否去重 / 统计范围 / 时间范围）")

    return errors, warnings


def _normalize_draft(obj: dict) -> dict:
    """把模型输出规范成草稿字段（缺字段给空串，类型不对给默认值）。"""
    ambiguities = obj.get("ambiguities")
    if not isinstance(ambiguities, list):
        ambiguities = []
    return {
        "key": str(obj.get("key") or "").strip().lower(),
        "name": str(obj.get("name") or "").strip(),
        "description": str(obj.get("description") or "").strip() or None,
        "formula": str(obj.get("formula") or "").strip(),
        "agg_kind_raw": str(obj.get("agg_kind") or "").strip().upper() or None,
        "ambiguities": [str(x).strip() for x in ambiguities if str(x).strip()][:6],
    }


# ── 试算（best-effort）────────────────────────────────────────────────────

def _as_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:  # Decimal / numpy 标量等
        return float(value)
    except (TypeError, ValueError):
        return None


def first_number(result: Any) -> float | int | None:
    """从查询结果里取第一个数值（行是 dict 或 list 都支持）。"""
    rows = getattr(result, "rows", None)
    if rows is None and isinstance(result, dict):
        rows = result.get("rows")
    for row in list(rows or [])[:3]:
        values = list(row.values()) if isinstance(row, dict) else (
            list(row) if isinstance(row, (list, tuple)) else [row]
        )
        for value in values:
            number = _as_number(value)
            if number is not None:
                return number
    return None


async def _try_sample(
    *, draft: dict, ds: DataSource, table_ref: str | None, user_id: UUID, db: AsyncSession
) -> tuple[float | int | None, str | None]:
    """用正式查询链路跑一次 LIMIT 5；失败只返回错误文案。"""
    try:
        metric = MetricDefinition(
            id=uuid4(),
            user_id=user_id,
            key=draft.get("key") or "draft_metric",
            name=draft.get("name") or "指标草稿",
            description=draft.get("description"),
            formula=draft["formula"],
            agg_kind=draft.get("agg_kind") or "SUM",
            datasource_id=ds.id,
            table_ref=table_ref,
            active=True,
        )
        config = resolve_metric(metric, datasource_id=ds.id, limit=SAMPLE_LIMIT)
        result = await execute_chart_query(
            datasource_id=ds.id, config=config, user_id=user_id, db=db, force=True,
        )
        value = first_number(result)
        if value is None:
            return None, "试算没有返回数值（可能过滤后无数据，或聚合结果为空）"
        return value, None
    except MetricServiceError as exc:
        return None, f"试算失败：{exc}"
    except Exception as exc:  # noqa: BLE001 - 试算是附加能力，绝不影响草稿返回
        logger.warning("[metric_ai] sample_failed key=%s error=%s", draft.get("key"), exc)
        return None, f"试算失败：{str(exc)[:200]}"


# ── 主入口 ────────────────────────────────────────────────────────────────

async def _load_datasource(
    db: AsyncSession, user_id: UUID, datasource_id: UUID | None
) -> DataSource:
    """取数据源并校验归属：指定 id 时必须属于当前用户；未指定时取该用户最新一个。"""
    if datasource_id is not None:
        result = await db.execute(
            select(DataSource).where(
                DataSource.id == datasource_id, DataSource.user_id == user_id
            )
        )
        ds = result.scalar_one_or_none()
        if ds is None:
            raise MetricDraftDatasourceNotFound("数据源不存在或不属于当前用户")
        return ds
    result = await db.execute(
        select(DataSource)
        .where(DataSource.user_id == user_id)
        .order_by(DataSource.created_at.desc())
        .limit(1)
    )
    ds = result.scalar_one_or_none()
    if ds is None:
        raise MetricDraftError("当前账号还没有数据源，请先上传数据或连接数据库")
    return ds


async def _load_visible_metrics(db: AsyncSession, user_id: UUID) -> list[Any]:
    """既有指标（自有 + 全局模板）——与 api/v1/metrics.py::_visible_scope 同义。

    service 层不反向 import API 模块，所以这里就地表达同一条谓词；
    两边都只读"可见范围"，改动时请同步（GET /metrics 与起草看到的是同一批 key）。
    """
    result = await db.execute(
        select(MetricDefinition.key, MetricDefinition.name, MetricDefinition.formula)
        .where(
            (MetricDefinition.user_id == user_id) | (MetricDefinition.user_id.is_(None)),
            MetricDefinition.active.is_(True),
        )
        .order_by(MetricDefinition.created_at.desc())
        .limit(MAX_EXISTING_METRICS)
    )
    return list(result.all())


async def _call_llm(messages: list[dict]) -> str:
    """调 LLM（JSON 模式）；两个已知异常映射 503，其余异常也按不可用处理（fail-closed）。"""
    try:
        return await LLMClient().complete(
            messages,
            temperature=0.2,
            max_tokens=900,
            response_format={"type": "json_object"},
            enable_thinking=False,
        )
    except (AINotConfiguredError, AIUpstreamError) as exc:
        raise MetricDraftUnavailable(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 网络/超时等同样视为 AI 不可用
        logger.warning("[metric_ai] llm_failed: %s", exc)
        raise MetricDraftUnavailable(f"AI 服务暂时不可用：{str(exc)[:200]}") from exc


async def draft_metric(
    *,
    prompt: str,
    datasource_id: UUID | None,
    table_ref: str | None,
    user_id: UUID,
    db: AsyncSession,
) -> dict:
    """生成指标草稿（不落库）。返回 MetricDraftResponse 的字段（snake_case）。"""
    text_prompt = str(prompt or "").strip()
    if not text_prompt:
        raise MetricDraftError("请先描述你要的指标（例如：已支付订单的销售额）")

    ds = await _load_datasource(db, user_id, datasource_id)
    meta = ds.schema_meta if isinstance(getattr(ds, "schema_meta", None), dict) else {}
    fields = [f for f in (meta.get("fields") or []) if isinstance(f, dict) and f.get("name")]
    if not fields:
        raise MetricDraftError(
            "该数据源的字段尚未同步，暂时无法起草指标（请先同步数据源 schema）"
        )
    field_types = {
        str(f["name"]): type_kind(norm_type(f.get("data_type") or f.get("type")))
        for f in fields
    }
    resolved_table_ref = (
        (table_ref or "").strip()
        or str(meta.get("table_name") or "").strip()
        or "data"
    )

    metric_rows = await _load_visible_metrics(db, user_id)
    existing_keys = {
        str(getattr(row, "key", None) or (row[0] if isinstance(row, (tuple, list)) else "")).strip()
        for row in metric_rows
    }
    existing_keys.discard("")

    messages = build_draft_messages(
        prompt=text_prompt,
        schema_block=_render_schema_block(ds, fields),
        metrics_block=_render_metrics_block(metric_rows),
    )

    # LLM 产出：解析失败重试 1 次（把错误回灌），再失败就返回带 errors 的草稿（200）
    parsed: dict | None = None
    parse_error = ""
    for attempt in range(2):
        content = await _call_llm(messages)
        try:
            parsed = extract_json_object(content)
            break
        except Exception as exc:  # noqa: BLE001
            parse_error = str(exc)
            logger.warning("[metric_ai] json_parse_failed attempt=%d error=%s", attempt + 1, exc)
            if attempt == 0:
                messages = [
                    *messages,
                    {"role": "assistant", "content": (content or "")[:2000]},
                    {
                        "role": "user",
                        "content": (
                            "上一次输出不是合法的 JSON 对象（" + parse_error + "）。"
                            "请只输出一个 JSON 对象：不要 markdown 代码块、不要解释文字。"
                        ),
                    },
                ]

    model_name = str(getattr(settings, "openai_model", "") or "")
    if parsed is None:
        return {
            "key": "",
            "name": "",
            "description": None,
            "formula": "",
            "agg_kind": None,
            "datasource_id": ds.id,
            "table_ref": resolved_table_ref,
            "references": [],
            "ambiguities": [],
            "validation": {
                "ok": False,
                "errors": [f"AI 返回不是合法 JSON（{parse_error[:120]}）"],
                "warnings": [],
            },
            "sample_value": None,
            "sample_error": None,
            "model": model_name,
        }

    draft = _normalize_draft(parsed)
    draft["agg_kind"] = derive_agg_kind(draft["formula"], draft.pop("agg_kind_raw", None))
    errors, warnings = validate_draft(
        draft, field_types=field_types, existing_keys=existing_keys
    )
    ok = not errors
    references = extract_references(draft["formula"]) if draft["formula"] else []

    sample_value: float | int | None = None
    sample_error: str | None = None
    if ok:  # 只有校验通过的公式才值得试算（避免对着被拒的 SQL 打数据源）
        sample_value, sample_error = await _try_sample(
            draft=draft, ds=ds, table_ref=resolved_table_ref, user_id=user_id, db=db,
        )

    return {
        "key": draft["key"],
        "name": draft["name"],
        "description": draft["description"],
        "formula": draft["formula"],
        "agg_kind": draft["agg_kind"],
        "datasource_id": ds.id,
        "table_ref": resolved_table_ref,
        "references": references,
        "ambiguities": draft["ambiguities"],
        "validation": {"ok": ok, "errors": errors, "warnings": warnings},
        "sample_value": sample_value,
        "sample_error": sample_error,
        "model": model_name,
    }
