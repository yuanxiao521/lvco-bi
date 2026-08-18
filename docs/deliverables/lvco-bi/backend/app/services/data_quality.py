import asyncio
import logging
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource, SourceType

log = logging.getLogger("lvco.data_quality")

_TABLE_NAME = "data"
_SAMPLE_LIMIT = 5
_NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL")
_STRING_TYPES = ("VARCHAR", "TEXT", "STRING")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


async def _resolve_schema(
    datasource_id: UUID | str,
    db: AsyncSession,
) -> tuple[str, str] | None:
    result = await db.execute(
        select(DataSource).where(DataSource.id == datasource_id)
    )
    ds = result.scalar_one_or_none()
    if ds is None:
        return None
    schema_name = duckdb_client.get_schema_name(ds.user_id, ds.id, ds.name)
    # For PG sources, use the actual table name from schema_meta
    table_name = _TABLE_NAME
    if ds.source_type == SourceType.postgresql and ds.schema_meta and isinstance(ds.schema_meta, dict):
        table_name = ds.schema_meta.get("table_name", _TABLE_NAME)
    return schema_name, table_name


def _empty(field: str | None = None, issue_type: str | None = None) -> dict:
    return {
        "field": field,
        "issue_type": issue_type,
        "count": 0,
        "percentage": 0.0,
        "sample_values": [],
    }


def _ensure_table_exists(schema_name: str, table_name: str) -> bool:
    rows = duckdb_client.fetchall(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema_name, table_name],
    )
    return bool(rows)


def _column_type(schema_name: str, table_name: str, field: str) -> str | None:
    rows = duckdb_client.fetchall(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? AND column_name = ?",
        [schema_name, table_name, field],
    )
    if not rows:
        return None
    return str(rows[0][0]).upper() if rows[0][0] is not None else None


def _is_numeric_type(data_type: str | None) -> bool:
    if not data_type:
        return False
    dt = data_type.upper()
    return any(n in dt for n in _NUMERIC_TYPES)


def _is_string_type(data_type: str | None) -> bool:
    if not data_type:
        return False
    dt = data_type.upper()
    return any(n in dt for n in _STRING_TYPES)


async def null_count(
    datasource_id: UUID | str,
    field: str,
    db: AsyncSession,
) -> dict:
    resolved = await _resolve_schema(datasource_id, db)
    if resolved is None:
        return _empty(field=field, issue_type="missing")
    schema_name, table_name = resolved

    def _run() -> dict:
        if not _ensure_table_exists(schema_name, table_name):
            return _empty(field=field, issue_type="missing")
        col = _column_type(schema_name, table_name, field)
        if col is None:
            return _empty(field=field, issue_type="missing")

        ident = _quote_ident(field)
        table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'
        sql = (
            f"SELECT "
            f"  CAST(SUM(CASE WHEN {ident} IS NULL THEN 1 ELSE 0 END) AS BIGINT) AS null_count, "
            f"  COUNT(*) AS total_count "
            f"FROM {table_ref}"
        )
        rows = duckdb_client.fetchall(sql)
        null_n = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        total = int(rows[0][1]) if rows and rows[0][1] else 0
        percentage = (null_n / total * 100.0) if total > 0 else 0.0

        sample_sql = (
            f"SELECT CAST({ident} AS VARCHAR) FROM {table_ref} "
            f"WHERE {ident} IS NULL LIMIT {_SAMPLE_LIMIT}"
        )
        sample_rows = duckdb_client.fetchall(sample_sql)
        sample_values = [r[0] for r in sample_rows]

        return {
            "field": field,
            "issue_type": "missing",
            "count": null_n,
            "percentage": round(percentage, 4),
            "sample_values": sample_values,
        }

    return await asyncio.to_thread(_run)


async def outlier_count(
    datasource_id: UUID | str,
    field: str,
    db: AsyncSession,
) -> dict:
    resolved = await _resolve_schema(datasource_id, db)
    if resolved is None:
        return _empty(field=field, issue_type="outlier")
    schema_name, table_name = resolved

    def _run() -> dict:
        if not _ensure_table_exists(schema_name, table_name):
            return _empty(field=field, issue_type="outlier")
        col = _column_type(schema_name, table_name, field)
        if col is None:
            return _empty(field=field, issue_type="outlier")
        if not _is_numeric_type(col):
            return _empty(field=field, issue_type="outlier")

        ident = _quote_ident(field)
        table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'
        sql = (
            f"WITH stats AS ("
            f"  SELECT AVG({ident}) AS mu, STDDEV_SAMP({ident}) AS sigma, COUNT(*) AS total_count "
            f"  FROM {table_ref} WHERE {ident} IS NOT NULL"
            f"), flagged AS ("
            f"  SELECT {ident} AS v FROM {table_ref}, stats "
            f"  WHERE {ident} IS NOT NULL AND stats.sigma IS NOT NULL AND stats.sigma > 0 "
            f"    AND ABS({ident} - stats.mu) / stats.sigma > 3"
            f") "
            f"SELECT (SELECT COUNT(*) FROM flagged) AS outlier_count, "
            f"       (SELECT total_count FROM stats) AS total_count, "
            f"       (SELECT ARRAY_AGG(v) FROM (SELECT v FROM flagged LIMIT {_SAMPLE_LIMIT})) AS samples"
        )
        rows = duckdb_client.fetchall(sql)
        if not rows:
            return _empty(field=field, issue_type="outlier")
        outlier_n = int(rows[0][0]) if rows[0][0] is not None else 0
        total = int(rows[0][1]) if rows[0][1] else 0
        samples_raw = rows[0][2]
        if isinstance(samples_raw, (list, tuple)):
            sample_values = [s for s in samples_raw]
        else:
            sample_values = []

        percentage = (outlier_n / total * 100.0) if total > 0 else 0.0

        return {
            "field": field,
            "issue_type": "outlier",
            "count": outlier_n,
            "percentage": round(percentage, 4),
            "sample_values": sample_values,
        }

    return await asyncio.to_thread(_run)


async def dup_row_count(
    datasource_id: UUID | str,
    db: AsyncSession,
) -> dict:
    resolved = await _resolve_schema(datasource_id, db)
    if resolved is None:
        return _empty(field=None, issue_type="duplicate")
    schema_name, table_name = resolved

    def _run() -> dict:
        if not _ensure_table_exists(schema_name, table_name):
            return _empty(field=None, issue_type="duplicate")

        table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'
        sql = (
            f"WITH row_counts AS ("
            f"  SELECT *, COUNT(*) OVER (PARTITION BY *) AS dup_cnt "
            f"  FROM {table_ref}"
            f"), totals AS ("
            f"  SELECT COUNT(*) AS total_count, "
            f"         COALESCE(SUM(CASE WHEN dup_cnt > 1 THEN dup_cnt - 1 ELSE 0 END), 0) AS dup_count "
            f"  FROM row_counts"
            f") "
            f"SELECT total_count, dup_count FROM totals"
        )
        try:
            rows = duckdb_client.fetchall(sql)
        except Exception:
            sql_simple = (
                f"SELECT COUNT(*) - COUNT(DISTINCT *) AS dup_count, COUNT(*) AS total_count FROM {table_ref}"
            )
            rows = duckdb_client.fetchall(sql_simple)

        if not rows:
            return _empty(field=None, issue_type="duplicate")
        dup_n = int(rows[0][0]) if rows[0][0] is not None else 0
        total = int(rows[0][1]) if rows[0][1] else 0
        percentage = (dup_n / total * 100.0) if total > 0 else 0.0

        sample_sql = (
            f"WITH d AS (SELECT *, COUNT(*) OVER () AS _cnt FROM {table_ref}) "
            f"SELECT * FROM d WHERE _cnt > 1 LIMIT {_SAMPLE_LIMIT}"
        )
        try:
            sample_rows = duckdb_client.fetchall(sample_sql)
        except Exception:
            sample_rows = []
        sample_values = [dict(row._mapping) if hasattr(row, "_mapping") else list(row) for row in sample_rows]

        return {
            "field": None,
            "issue_type": "duplicate",
            "count": dup_n,
            "percentage": round(percentage, 4),
            "sample_values": sample_values,
        }

    return await asyncio.to_thread(_run)


async def format_issue_count(
    datasource_id: UUID | str,
    field: str,
    db: AsyncSession,
) -> dict:
    resolved = await _resolve_schema(datasource_id, db)
    if resolved is None:
        return _empty(field=field, issue_type="format")
    schema_name, table_name = resolved

    def _run() -> dict:
        if not _ensure_table_exists(schema_name, table_name):
            return _empty(field=field, issue_type="format")
        col = _column_type(schema_name, table_name, field)
        if col is None:
            return _empty(field=field, issue_type="format")
        if not _is_string_type(col):
            return _empty(field=field, issue_type="format")

        ident = _quote_ident(field)
        table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'

        sql = (
            f"WITH non_null AS ("
            f"  SELECT CAST({ident} AS VARCHAR) AS v FROM {table_ref} WHERE {ident} IS NOT NULL"
            f"), total_count AS (SELECT COUNT(*) AS total_count FROM non_null), "
            f"bad AS ("
            f"  SELECT v FROM non_null "
            f"  WHERE NOT regexp_matches(v, '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$')"
            f") "
            f"SELECT (SELECT COUNT(*) FROM bad) AS bad_count, "
            f"       (SELECT total_count FROM total_count) AS total_count, "
            f"       (SELECT LIST(v) FROM (SELECT v FROM bad LIMIT {_SAMPLE_LIMIT})) AS samples"
        )
        rows = duckdb_client.fetchall(sql)
        if not rows:
            return _empty(field=field, issue_type="format")
        bad_n = int(rows[0][0]) if rows[0][0] is not None else 0
        total = int(rows[0][1]) if rows[0][1] else 0
        samples_raw = rows[0][2]
        if isinstance(samples_raw, (list, tuple)):
            sample_values = list(samples_raw)
        else:
            sample_values = []
        percentage = (bad_n / total * 100.0) if total > 0 else 0.0

        return {
            "field": field,
            "issue_type": "format",
            "count": bad_n,
            "percentage": round(percentage, 4),
            "sample_values": sample_values,
        }

    return await asyncio.to_thread(_run)