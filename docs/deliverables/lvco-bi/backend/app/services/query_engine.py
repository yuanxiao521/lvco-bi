import hashlib
import json
import structlog
import time
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource, SourceType
from app.schemas.query import ChartQueryConfig, QueryResult
from app.utils.crypto import decrypt_value, get_encryption_key
from app.services.cache_service import cache

ALLOWED_AGGREGATIONS = frozenset({"SUM", "AVG", "COUNT", "MAX", "MIN", "STDDEV", "MEDIAN", "COUNT_DISTINCT"})
ALLOWED_OPERATORS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "like"})

logger = structlog.get_logger("query_engine")

class QueryEngineError(Exception):
    def __init__(self, message: str, code: str = "QUERY_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def _make_schema_name(user_id: UUID, datasource_id: UUID) -> str:
    uid = str(user_id).replace("-", "")[:8]
    did = str(datasource_id).replace("-", "")[:8]
    return f"{uid}_{did}"


def _decrypt_connection_info(source_type: SourceType, connection_config: dict | None) -> dict:
    """Return a copy of connection_config with decrypted password if applicable."""
    conn_info = dict(connection_config) if connection_config else {}
    if source_type in (SourceType.mysql, SourceType.postgresql):
        key = get_encryption_key()
        if key and conn_info.get("password"):
            conn_info["password"] = decrypt_value(conn_info["password"], key)
    return conn_info


def _get_table_columns(schema_name: str) -> list[tuple[str, str]]:
    rows = duckdb_client.fetchall(
        f"SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema = ? AND table_name = 'data' "
        f"ORDER BY ordinal_position",
        [schema_name]
    )
    return [(row[0], row[1]) for row in rows]


def _ensure_datasource_ready(schema_name: str, source_type: SourceType | None = None, pg_table_name: str = "data") -> set[str]:
    if source_type in (SourceType.mysql, SourceType.postgresql):
        # 对于 PostgreSQL/MySQL 外部数据源，DuckDB 的 information_schema.tables
        # 无法正确查到底层数据库的表（DuckDB PostgreSQL scanner 的已知问题）。
        # 必须通过三部分名查询 PostgreSQL 自己的 information_schema。
        try:
            rows = duckdb_client.fetchall(
                f'SELECT table_name FROM "{schema_name}".information_schema.tables '
                f'WHERE table_schema = ? AND table_type = ?',
                ["public", "BASE TABLE"]
            )
            tables = [row[0] for row in rows]
        except Exception:
            tables = []
        if not tables:
            raise QueryEngineError("数据源未连接或未同步，请先测试连接并同步", code="NOT_FOUND")
        # 获取指定表的列信息（同样使用三部分名）
        rows = duckdb_client.fetchall(
            f'SELECT column_name, data_type FROM "{schema_name}".information_schema.columns '
            f'WHERE table_schema = ? AND table_name = ? '
            f'ORDER BY ordinal_position',
            ["public", pg_table_name]
        )
        return {row[0] for row in rows} if rows else set()

    tables = duckdb_client.fetchall(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = 'data'",
        [schema_name]
    )
    if not tables:
        raise QueryEngineError("数据源不存在或未初始化，请先上传文件", code="NOT_FOUND")

    columns = _get_table_columns(schema_name)
    if not columns:
        raise QueryEngineError("数据源表结构为空", code="SCHEMA_NOT_READY")

    return {col[0] for col in columns}


def _validate_fields(fields: list[str], schema_fields: set[str], context: str) -> list[str]:
    """校验字段，返回修正大小写后的字段列表"""
    schema_lower = {f.lower(): f for f in schema_fields}
    corrected: list[str] = []
    for f in fields:
        actual = schema_lower.get(f.lower())
        if actual is None:
            sorted_fields = sorted(schema_fields)
            # 显示全部可用字段（上限50个），避免用户看不到完整列表
            field_list = sorted_fields if len(sorted_fields) <= 50 else sorted_fields[:50]
            total = len(sorted_fields)
            suffix = f" ...（共{total}个字段）" if total > 50 else f"（共{total}个字段）"
            hint = f"，可用字段: {', '.join(field_list)}{suffix}" if schema_fields else ""
            raise QueryEngineError(
                f"{context}字段 '{f}' 不存在于数据源中{hint}",
                code="INVALID_FIELD",
            )
        corrected.append(actual)
    return corrected


def _validate_aggregations(aggs: list[str]) -> None:
    for agg in aggs:
        if agg.upper() not in ALLOWED_AGGREGATIONS:
            raise QueryEngineError(
                f"不支持的聚合函数: {agg}，仅支持 {', '.join(sorted(ALLOWED_AGGREGATIONS))}",
                code="INVALID_AGGREGATION",
            )


def _build_select(dimensions: list[str], measures: list[dict[str, str]]) -> tuple[str, list[str]]:
    parts: list[str] = []
    result_columns: list[str] = []

    for dim in dimensions:
        parts.append(f'"{dim}"')
        result_columns.append(dim)

    for m in measures:
        field = m["field"]
        agg = m["agg"].upper()
        alias = f"{agg.lower()}_{field}"
        if agg == "COUNT_DISTINCT":
            parts.append(f'COUNT(DISTINCT "{field}") AS "{alias}"')
        elif agg == "STDDEV":
            parts.append(f'STDDEV("{field}") AS "{alias}"')
        elif agg == "MEDIAN":
            parts.append(f'MEDIAN("{field}") AS "{alias}"')
        else:
            parts.append(f'{agg}("{field}") AS "{alias}"')
        result_columns.append(field)

    return ", ".join(parts), result_columns


def _build_where(filters: list[dict[str, Any]], schema_fields: set[str], params: list[Any]) -> str:
    if not filters:
        return ""

    schema_lower = {f.lower(): f for f in schema_fields}
    conditions: list[str] = []
    for f in filters:
        field = f["field"]
        op = f["op"]
        value = f["value"]

        actual = schema_lower.get(field.lower())
        if actual is None:
            raise QueryEngineError(f"筛选字段 '{field}' 不存在于数据源中", code="INVALID_FIELD")
        # 使用实际列名
        field = actual
        f["field"] = actual
        if op not in ALLOWED_OPERATORS:
            raise QueryEngineError(
                f"不支持的操作符: {op}，仅支持 {', '.join(sorted(ALLOWED_OPERATORS))}",
                code="INVALID_OPERATOR",
            )

        if op == "eq":
            conditions.append(f'"{field}" = ?')
            params.append(value)
        elif op == "neq":
            conditions.append(f'"{field}" != ?')
            params.append(value)
        elif op == "gt":
            conditions.append(f'"{field}" > ?')
            params.append(value)
        elif op == "gte":
            conditions.append(f'"{field}" >= ?')
            params.append(value)
        elif op == "lt":
            conditions.append(f'"{field}" < ?')
            params.append(value)
        elif op == "lte":
            conditions.append(f'"{field}" <= ?')
            params.append(value)
        elif op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise QueryEngineError(
                    "between 操作符的 value 必须是包含两个元素的数组",
                    code="INVALID_FILTER",
                )
            conditions.append(f'"{field}" BETWEEN ? AND ?')
            params.extend(value)
        elif op == "in":
            if not isinstance(value, list) or len(value) == 0:
                raise QueryEngineError(
                    "in 操作符的 value 必须是非空数组",
                    code="INVALID_FILTER",
                )
            placeholders = ", ".join(["?"] * len(value))
            conditions.append(f'"{field}" IN ({placeholders})')
            params.extend(value)
        elif op == "like":
            conditions.append(f'"{field}" LIKE ?')
            params.append(value)

    return "WHERE " + " AND ".join(conditions)


def _build_group_by(dimensions: list[str]) -> str:
    if not dimensions:
        return ""
    return "GROUP BY " + ", ".join(f'"{dim}"' for dim in dimensions)


def _build_order_by(sort: dict[str, str] | None, measures: list[dict[str, str]]) -> str:
    if sort:
        field = sort["field"]
        order = sort.get("order", "desc").upper()
        if order not in ("ASC", "DESC"):
            order = "DESC"
        return f'ORDER BY "{field}" {order}'

    if measures:
        first = measures[0]
        alias = f'{first["agg"].upper().lower()}_{first["field"]}'
        return f'ORDER BY "{alias}" DESC'

    return ""


async def execute_chart_query(
    datasource_id: UUID,
    config: ChartQueryConfig,
    user_id: UUID,
    db: AsyncSession | None = None,
) -> QueryResult:
    schema_name = _make_schema_name(user_id, datasource_id)

    # For MySQL/PostgreSQL datasources, ATTACH the external database via DuckDB.
    source_type: SourceType | None = None
    pg_table_name: str = "data"  # default for CSV/Excel; overridden for PG
    if db is not None:
        result = await db.execute(select(DataSource).where(DataSource.id == datasource_id, DataSource.user_id == user_id))
        datasource = result.scalar_one_or_none()
        if datasource:
            source_type = datasource.source_type
            if source_type in (SourceType.mysql, SourceType.postgresql):
                # 尝试 ATTACH；若 schema 已存在则先 DETACH 重建连接（持久化文件会残留旧 schema 记录但连接已失效）
                try:
                    duckdb_client.execute(f'DETACH "{schema_name}"')
                except Exception:
                    pass
                conn_info = _decrypt_connection_info(source_type, datasource.connection_config)
                if source_type == SourceType.mysql:
                    from app.connectors.mysql_connector import mysql_connector as mysql_conn
                    attach_sql = mysql_conn.get_attach_sql(conn_info, schema_name)
                else:
                    from app.connectors.postgres_connector import postgres_connector as pg_conn
                    attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
                duckdb_client.execute(attach_sql)
                # Determine the correct table name for PG sources
                if datasource.schema_meta and isinstance(datasource.schema_meta, dict):
                    pg_table_name = datasource.schema_meta.get("table_name", "data")

    schema_fields = _ensure_datasource_ready(schema_name, source_type, pg_table_name)

    # 同步 schemaMeta：DuckDB 重启后实际列可能与持久化的 schemaMeta 不一致，
    # 导致前端 FieldPanel 显示的字段和实际可用列不同，自动修正
    if db is not None and datasource and schema_fields:
        stored_field_names: set[str] = set()
        if datasource.schema_meta and isinstance(datasource.schema_meta, dict):
            for f in (datasource.schema_meta.get("fields") or []):
                if isinstance(f, dict) and f.get("name"):
                    stored_field_names.add(f["name"])
        if stored_field_names != schema_fields:
            old_map: dict[str, dict] = {}
            if datasource.schema_meta and isinstance(datasource.schema_meta, dict):
                for f in (datasource.schema_meta.get("fields") or []):
                    if isinstance(f, dict) and f.get("name"):
                        old_map[f["name"]] = f
            new_fields = []
            for col_name in sorted(schema_fields):
                if col_name in old_map:
                    new_fields.append(old_map[col_name])
                else:
                    new_fields.append({
                        "name": col_name, "data_type": "VARCHAR",
                        "nullable": True, "category": "dimension",
                        "display_name": col_name,
                    })
            new_meta = dict(datasource.schema_meta) if isinstance(datasource.schema_meta, dict) else {}
            new_meta["fields"] = new_fields
            datasource.schema_meta = new_meta
            await db.flush()

    # --- cache lookup ---
    config_dict = config.model_dump()
    config_hash = hashlib.md5(json.dumps(config_dict, sort_keys=True, default=str).encode()).hexdigest()
    cache_key = f"query:{datasource_id}:{user_id}:{config_hash}"
    cached = cache.get(cache_key)
    if cached:
        data = json.loads(cached)
        return QueryResult(**data)

    measure_dicts = [m.model_dump() for m in config.measures]
    filter_dicts = [f.model_dump() for f in config.filters]

    # 校验并修正大小写（DuckDB 列名可能跟前端不一致）
    dims = _validate_fields(config.dimensions, schema_fields, "维度")
    meas_fields = _validate_fields([m["field"] for m in measure_dicts], schema_fields, "度量")
    for i, actual in enumerate(meas_fields):
        measure_dicts[i]["field"] = actual
    _validate_aggregations([m["agg"] for m in measure_dicts])

    select_clause, result_columns = _build_select(dims, measure_dicts)

    params: list[Any] = []
    where_clause = _build_where(filter_dicts, schema_fields, params)

    group_by_clause = _build_group_by(dims)

    sort_dict = config.sort.model_dump() if config.sort else None
    order_by_clause = _build_order_by(sort_dict, measure_dicts)

    if source_type in (SourceType.mysql, SourceType.postgresql):
        table_ref = f'"{schema_name}".public."{pg_table_name}"'
    else:
        table_ref = f'"{schema_name}"."data"'
    sql = (
        f"SELECT {select_clause} "
        f"FROM {table_ref} "
        f"{where_clause} "
        f"{group_by_clause} "
        f"{order_by_clause} "
        f"LIMIT {config.limit}"
    ).strip()

    # Auto-cap queries without LIMIT
    if "LIMIT" not in sql.upper():
        sql = sql.rstrip().rstrip(';').rstrip() + "\nLIMIT 500"
        logger.warning("auto_limit", datasource_id=str(datasource_id), reason="query missing LIMIT", capped_at=500)

    start_time = time.time()

    try:
        rows_raw = duckdb_client.fetchall(sql, params)
    except Exception as e:
        raise QueryEngineError(f"查询执行失败: {str(e)}", code="QUERY_EXECUTION_ERROR") from e

    query_time_ms = int((time.time() - start_time) * 1000)

    rows: list[dict[str, Any]] = []
    for row in rows_raw:
        row_dict: dict[str, Any] = {}
        for i, col in enumerate(result_columns):
            row_dict[col] = row[i] if i < len(row) else None
        rows.append(row_dict)

    result = QueryResult(
        columns=result_columns,
        rows=rows,
        chart_type=config.chart_type,
        query_time_ms=query_time_ms,
    )

    # --- cache set ---
    cache.set(cache_key, json.dumps(result.model_dump(mode="json"), default=str))

    return result
