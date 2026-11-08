"""数据画像服务（Data Profile）—— 让 Agent 看见"数据长什么样"。

为什么需要
----------
改造前，Agent 拿到的数据上下文只有"5 行样本 + 字段名/类型"：没有行数、没有基数
（distinct_count）、没有空值率、没有时间范围。模型因此在两件事上系统性犯错：

1. 维度选错：把 100 万行的明细列当低基数维度 GROUP BY，或反过来对只有 5 个取值的
   列做"按用户拆分"；
2. 聚合选错：对 99% 为空的值做均值、对 ID 列做 SUM、对时间列当字符串比较。

本模块只做一件事：给一个数据源算一份"画像"，且**诚实地说明这份画像是不是全量**。

成本控制（重要）
----------------
大表上 COUNT(DISTINCT) 是全表扫描，一张千万行表能把 DuckDB/远端库拖死。策略：

1. 先 `COUNT(*)` 判断行数（这一步必须真实，否则"总行数"会撒谎）；
2. 行数 > SAMPLE_ROW_THRESHOLD 时，**字段统计降级为采样**：优先
   `FROM t USING SAMPLE n ROWS`（reservoir，更接近无偏），某些表不支持采样
   （如远程 ATTACH 的 PostgreSQL 表）时回退到 `(SELECT * FROM t LIMIT n)`，
   并在返回里标 `sampled: true` + `sample_size`，**不假装是全量**；
3. `row_count` 永远是真实 COUNT(*) 的结果，不受采样影响；
4. 字段数量上限 MAX_PROFILE_FIELDS，避免一次画像打上千条 SQL。

敏感列
------
`sensitive_columns` 治理（见 app/services/sensitive_columns.py）要求敏感列的值
不进 LLM 上下文。所以敏感列**只给统计量**（null_rate / distinct_count / min / max），
**绝不出 sample_values**，且不执行任何把原始值取回来的查询。

安全说明
--------
表引用统一走 `table_ref_for()`（标识符已转义），列名一律来自 schema_meta 或
DuckDB information_schema 的白名单，并且只接受"已知列"——外部传入的 fields
参数只是筛选已知列，不会被拼进 SQL。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.attach_sql import is_external_source, sql_ident
from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource
from app.services.datasource_connection import ensure_attached, table_ref_for
from app.services.sensitive_columns import detect_sensitive

log = logging.getLogger("lvco.datasource_profile")

# ── 成本控制阈值 ──────────────────────────────────────────────────────
#: 超过该行数时字段统计降级为采样（COUNT(*) 仍为真实值）
SAMPLE_ROW_THRESHOLD = 1_000_000
#: 采样时实际统计的行数上限
SAMPLE_ROWS = 100_000
#: 单次画像最多统计的字段数（防止一次打上千条 SQL）
MAX_PROFILE_FIELDS = 60
#: 敏感列/普通列的样例值条数上限（避免 sample_limit 被放大成全表扫描）
MAX_SAMPLE_LIMIT = 20

_NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT",
                  "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC")
_TEMPORAL_TYPES = ("DATE", "TIMESTAMP", "DATETIME", "TIME")
_STRING_TYPES = ("VARCHAR", "TEXT", "STRING", "CHAR", "BLOB")


class DatasourceProfileError(Exception):
    """画像失败（带机器可读 code，供 API 映射 HTTP 状态码）。"""

    def __init__(self, message: str, *, code: str = "PROFILE_FAILED") -> None:
        super().__init__(message)
        self.code = code


class DatasourceNotFoundError(DatasourceProfileError):
    """数据源不存在或不属于该用户（对外一律 404，不区分两种情况）。"""

    def __init__(self, message: str = "数据源不存在") -> None:
        super().__init__(message, code="NOT_FOUND")


class InvalidFieldError(DatasourceProfileError):
    """fields 参数非法（未知字段 / 超出数量上限）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_FIELD")


# ── 类型判定 / 值序列化 ──────────────────────────────────────────────

def _norm_type(dtype: Any) -> str:
    return str(dtype or "").upper()


def _is_numeric(dtype: str) -> bool:
    return any(t in dtype for t in _NUMERIC_TYPES)


def _is_temporal(dtype: str) -> bool:
    return any(t in dtype for t in _TEMPORAL_TYPES)


def _supports_range(dtype: str) -> bool:
    """MIN/MAX 是否对"业务有意义"。

    数值列给范围；时间列给时间范围（Agent 最需要的"数据覆盖到哪天"）；
    字符串列不给 —— 字典序的最小/最大值对分析没有意义，给了反而误导。
    """
    return _is_numeric(dtype) or _is_temporal(dtype)


def _json_safe(value: Any) -> Any:
    """把 DuckDB 返回的 Python 值转成可 JSON 序列化的形式。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def _quote(name: str) -> str:
    """列名转义（与 data_quality._quote_ident 同语义）。"""
    return '"' + str(name).replace('"', '""') + '"'


# ── 数据源解析 ────────────────────────────────────────────────────────

async def _load_owned_datasource(
    db_session: AsyncSession, user_id: UUID | str, datasource_id: UUID | str
) -> DataSource | None:
    """按 (id, user_id) 取数据源 —— 属主校验不在 API 层独占，服务层再兜一次。"""
    stmt = select(DataSource).where(
        DataSource.id == datasource_id, DataSource.user_id == user_id
    )
    result = await db_session.execute(stmt)
    return result.scalar_one_or_none()


def _resolve_target(ds: DataSource) -> tuple[str, str, dict[str, Any]]:
    """解析 (schema_name, table_ref, schema_meta)。"""
    meta = ds.schema_meta if isinstance(ds.schema_meta, dict) else {}
    source_type = ds.source_type
    # 与查图链路（query_engine.execute_chart_query）保持一致：不带 db_name，
    # 否则同一数据源在"同步"与"查询"两条路径上会算出两个不同的 ATTACH 别名。
    schema_name = duckdb_client.get_schema_name(ds.user_id, ds.id, ds.name or "")
    table_name = meta.get("table_name") or "data"
    table_ref = table_ref_for(schema_name, source_type, table_name)
    return schema_name, table_ref, meta


def _columns_from_duckdb(
    schema_name: str, source_type: Any, table_name: str
) -> list[dict[str, Any]]:
    """schema_meta 缺字段时，从 DuckDB/远端 information_schema 兜底读取。"""
    stype = str(getattr(source_type, "value", source_type)).lower()
    if stype == "postgresql":
        # 外部 PG：DuckDB 的 information_schema 查不到远端列，必须走三段名
        sql = (
            f"SELECT column_name, data_type FROM {sql_ident(schema_name)}"
            ".information_schema.columns WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position"
        )
        params: list[Any] = ["public", table_name]
    elif stype == "mysql":
        # MySQL 的 table_schema 是库名（尚未同步时未知），只用 table_name 过滤
        sql = (
            f"SELECT column_name, data_type FROM {sql_ident(schema_name)}"
            ".information_schema.columns WHERE table_name = ? ORDER BY ordinal_position"
        )
        params = [table_name]
    else:
        sql = (
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = 'data' ORDER BY ordinal_position"
        )
        params = [schema_name]
    rows = duckdb_client.fetchall(sql, params)
    return [
        {"name": row[0], "data_type": str(row[1] or "").upper()}
        for row in rows or []
        if row and row[0]
    ]


def _field_meta(ds: DataSource, meta: dict[str, Any], schema_name: str) -> list[dict[str, Any]]:
    """字段清单（name/type/sensitive）。优先用已同步的 schema_meta。"""
    raw = [f for f in (meta.get("fields") or []) if isinstance(f, dict) and f.get("name")]
    if not raw:
        raw = _columns_from_duckdb(schema_name, ds.source_type, meta.get("table_name") or "data")
    out: list[dict[str, Any]] = []
    for field in raw:
        name = str(field["name"])
        dtype = _norm_type(field.get("data_type") or field.get("type"))
        # 手工标记优先（前端字段编辑器里勾过的 sensitive），否则按列名启发式识别
        if "sensitive" in field:
            sensitive = bool(field.get("sensitive"))
        else:
            sensitive = bool(detect_sensitive(name))
        out.append({"name": name, "type": dtype, "sensitive": sensitive})
    return out


def _select_fields(
    all_fields: list[dict[str, Any]], requested: list[str] | None
) -> list[dict[str, Any]]:
    """按请求筛选字段；未知字段直接报错（拒绝静默吞掉，避免调用方以为算过了）。"""
    if not requested:
        if len(all_fields) > MAX_PROFILE_FIELDS:
            raise InvalidFieldError(
                f"字段数 {len(all_fields)} 超过画像上限 {MAX_PROFILE_FIELDS}，请用 fields 指定要画像的列"
            )
        return all_fields

    by_name = {f["name"]: f for f in all_fields}
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for name in requested:
        key = str(name)
        if key in seen:
            continue
        seen.add(key)
        field = by_name.get(key)
        if field is None:
            unknown.append(key)
            continue
        picked.append(field)
    if unknown:
        raise InvalidFieldError(f"字段不存在：{', '.join(unknown)}")
    if not picked:
        raise InvalidFieldError("fields 为空，无法画像")
    if len(picked) > MAX_PROFILE_FIELDS:
        raise InvalidFieldError(f"字段数 {len(picked)} 超过画像上限 {MAX_PROFILE_FIELDS}")
    return picked


# ── 采样 ──────────────────────────────────────────────────────────────

def _count_rows(table_ref: str) -> int:
    """真实总行数（不做采样，这是"这份数据有多大"的唯一诚实来源）。"""
    rows = duckdb_client.fetchall(f"SELECT COUNT(*) FROM {table_ref}")
    if not rows or rows[0][0] is None:
        return 0
    return int(rows[0][0])


def _stat_source(table_ref: str, row_count: int) -> tuple[str, int, bool]:
    """决定字段统计的取样来源，返回 (FROM 片段, 参与统计行数, 是否采样)。

    优先 `USING SAMPLE n ROWS`（reservoir 采样，偏差小于取前 N 行）；
    远程 ATTACH 表可能不支持采样语法，退化为 LIMIT 子查询并如实返回 sampled=True。
    """
    if row_count <= SAMPLE_ROW_THRESHOLD:
        return table_ref, row_count, False
    n = max(1, min(SAMPLE_ROWS, row_count))
    # 注意：采样必须先包成子查询再拼 WHERE/DISTINCT。DuckDB 不接受
    # "FROM t USING SAMPLE n ROWS WHERE ..."（Parser Error: syntax error at or near "WHERE"），
    # 直接把 USING SAMPLE 当 FROM 后缀拼进带 WHERE 的查询会整条失败。
    try:
        duckdb_client.fetchall(f"SELECT COUNT(*) FROM {table_ref} USING SAMPLE {n} ROWS")
        return f"(SELECT * FROM {table_ref} USING SAMPLE {n} ROWS)", n, True
    except Exception as exc:  # noqa: BLE001 —— 采样语法不支持属预期内降级
        log.info("profile_sample_fallback table=%s err=%s", table_ref, str(exc)[:120])
        return f"(SELECT * FROM {table_ref} LIMIT {n})", n, True


# ── 单字段统计 ────────────────────────────────────────────────────────

def _profile_field(field: dict[str, Any], src: str, sample_limit: int) -> dict[str, Any]:
    """算一个字段的统计量。单列失败不影响其它列（返回 error 字段而非整体崩）。"""
    name = field["name"]
    dtype = field.get("type") or ""
    sensitive = bool(field.get("sensitive"))
    ident = _quote(name)

    item: dict[str, Any] = {
        "name": name,
        "type": dtype,
        "sensitive": sensitive,
        "null_rate": None,
        "distinct_count": None,
        "min": None,
        "max": None,
        "sample_values": [],
    }
    try:
        # 一行 SQL 算完非空数/基数/极值：比"每指标一条 SQL"少扫 4 次
        rows = duckdb_client.fetchall(
            "SELECT COUNT(*) AS total, "
            f"COUNT({ident}) AS non_null, "
            f"COUNT(DISTINCT {ident}) AS distinct_cnt, "
            f"MIN({ident}) AS min_v, "
            f"MAX({ident}) AS max_v "
            f"FROM {src}"
        )
        if not rows:
            item["error"] = "统计查询无返回"
            return item
        total = int(rows[0][0] or 0)
        non_null = int(rows[0][1] or 0)
        item["null_rate"] = round((total - non_null) / total, 6) if total > 0 else 0.0
        item["distinct_count"] = int(rows[0][2] or 0)
        if _supports_range(dtype):
            item["min"] = _json_safe(rows[0][3])
            item["max"] = _json_safe(rows[0][4])
    except Exception as exc:  # noqa: BLE001
        log.warning("profile_field_failed field=%s err=%s", name, str(exc)[:160])
        item["error"] = str(exc)[:200]
        return item

    # 敏感列：不取任何原始值（样例值是最容易被泄漏的形态）
    if sensitive:
        return item

    try:
        sample_rows = duckdb_client.fetchall(
            f"SELECT DISTINCT CAST({ident} AS VARCHAR) FROM {src} "
            f"WHERE {ident} IS NOT NULL LIMIT {int(sample_limit)}"
        )
        item["sample_values"] = [row[0] for row in sample_rows or [] if row and row[0] is not None]
    except Exception as exc:  # noqa: BLE001
        log.info("profile_sample_failed field=%s err=%s", name, str(exc)[:160])

    return item


def _profile_sync(table_ref: str, fields: list[dict[str, Any]], sample_limit: int) -> dict[str, Any]:
    """同步部分（DuckDB 调用是阻塞的，由调用方放进 to_thread）。"""
    row_count = _count_rows(table_ref)
    src, sample_size, sampled = _stat_source(table_ref, row_count)
    items = [_profile_field(f, src, sample_limit) for f in fields]
    return {
        "row_count": row_count,
        "sampled": sampled,
        "sample_size": sample_size,
        "fields": items,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 对外入口 ──────────────────────────────────────────────────────────

async def profile_datasource(
    db_session: AsyncSession,
    user_id: UUID | str,
    datasource_id: UUID | str,
    *,
    fields: list[str] | None = None,
    sample_limit: int = 5,
) -> dict[str, Any]:
    """给数据源算一份画像。

    Args:
        db_session: 异步会话（只用于读取 DataSource，不写入）。
        user_id: 当前用户，用于属主校验（非属主与不存在一律 DatasourceNotFoundError）。
        datasource_id: 数据源 ID。
        fields: 只画像这些列（必须是已同步的已知列）；None 表示全部（受 MAX_PROFILE_FIELDS 限制）。
        sample_limit: 每列返回的样例值条数（1..MAX_SAMPLE_LIMIT）。

    Returns:
        {
          "row_count": int,            # 真实总行数
          "sampled": bool,             # 字段统计是否为采样
          "sample_size": int,          # 实际参与统计的行数（非采样时 == row_count）
          "fields": [{"name", "type", "sensitive", "null_rate", "distinct_count",
                      "min", "max", "sample_values"}],
          "generated_at": ISO8601 UTC
        }

    Raises:
        DatasourceNotFoundError: 数据源不存在或不属于该用户。
        InvalidFieldError: fields 含未知列 / 超上限。
        DatasourceProfileError: 其余画像失败。
    """
    ds = await _load_owned_datasource(db_session, user_id, datasource_id)
    if ds is None:
        raise DatasourceNotFoundError("数据源不存在")

    limit = max(1, min(int(sample_limit or 5), MAX_SAMPLE_LIMIT))
    schema_name, table_ref, meta = _resolve_target(ds)

    # 外部库（MySQL/PG）可能还没 ATTACH（例如进程刚重启）——这里补一次，与查图链路一致
    if is_external_source(ds.source_type):
        try:
            await asyncio.to_thread(ensure_attached, ds, schema_name)
        except Exception as exc:  # noqa: BLE001
            raise DatasourceProfileError(
                f"数据源未连接或不可用：{str(exc)[:200]}", code="ATTACH_FAILED"
            ) from exc

    all_fields = await asyncio.to_thread(_field_meta, ds, meta, schema_name)
    if not all_fields:
        raise DatasourceProfileError("数据源没有可用字段，请先同步", code="SCHEMA_EMPTY")
    picked = _select_fields(all_fields, fields)

    try:
        payload = await asyncio.to_thread(_profile_sync, table_ref, picked, limit)
    except DatasourceProfileError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("profile_failed table=%s err=%s", table_ref, str(exc)[:200])
        raise DatasourceProfileError(f"画像失败：{str(exc)[:200]}") from exc

    log.info(
        "profile_done datasource=%s rows=%s sampled=%s fields=%s",
        datasource_id, payload["row_count"], payload["sampled"], len(payload["fields"]),
    )
    return payload