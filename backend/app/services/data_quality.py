"""DataQualityService - 数据质量检测服务（Data Cleaning AI 的内部依赖）。

重构后使用 DataSourceSchemaRepository 加载数据源元数据，自身只负责：
1. 在 DuckDB 中执行 SQL 检测
2. 采样 + 统计封装
3. 结果聚合

业务模式：
- get_by_id/datasource_id 注入一个只读 Repository
- 检测结果以 dict 返回（前端展示需要灵活字段）
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from uuid import UUID

from app.core.duckdb_client import duckdb_client
from app.repositories.datasource_schema_repository import (
    SQLAlchemyDataSourceSchemaRepository,
)

log = logging.getLogger("lvco.data_quality")

_SAMPLE_LIMIT = 5
_NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "FLOAT", "DOUBLE", "REAL", "DECIMAL")
_STRING_TYPES = ("VARCHAR", "TEXT", "STRING")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _quote_ident(name: str) -> str:
    """对 SQL 标识符（表名、字段名）进行转义，防止 SQL 注入或关键字冲突。"""
    return '"' + str(name).replace('"', '""') + '"'


class DataQualityService:
    """数据质量检测服务。

    提供 6 类检测：missing / outlier_iqr / outlier / type_mismatch / duplicate / format。
    """

    def __init__(self, schema_repo: SQLAlchemyDataSourceSchemaRepository) -> None:
        self.schema_repo = schema_repo

    async def _resolve(self, datasource_id: UUID | str) -> tuple[str, str] | None:
        """解析数据源 → (schema_name, table_name)。"""
        info = await self.schema_repo.get_schema_info(datasource_id)
        if info is None:
            return None
        schema_name, table_name, _ = info
        return schema_name, table_name

    @staticmethod
    def _empty(field: str | None = None, issue_type: str | None = None) -> dict:
        return {
            "field": field,
            "issue_type": issue_type,
            "count": 0,
            "percentage": 0.0,
            "sample_values": [],
        }

    @staticmethod
    def _ensure_table_exists(schema_name: str, table_name: str) -> bool:
        rows = duckdb_client.fetchall(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [schema_name, table_name],
        )
        return bool(rows)

    @staticmethod
    def _column_type(schema_name: str, table_name: str, field: str) -> str | None:
        rows = duckdb_client.fetchall(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? AND column_name = ?",
            [schema_name, table_name, field],
        )
        if not rows:
            return None
        return str(rows[0][0]).upper() if rows[0][0] is not None else None

    @staticmethod
    def _is_numeric_type(data_type: str | None) -> bool:
        if not data_type:
            return False
        dt = data_type.upper()
        return any(n in dt for n in _NUMERIC_TYPES)

    @staticmethod
    def _is_string_type(data_type: str | None) -> bool:
        if not data_type:
            return False
        dt = data_type.upper()
        return any(n in dt for n in _STRING_TYPES)

    async def null_count(self, datasource_id: UUID | str, field: str) -> dict:
        """检测指定字段中的空值（NULL）数量及占比。"""
        resolved = await self._resolve(datasource_id)
        if resolved is None:
            return self._empty(field=field, issue_type="missing")
        schema_name, table_name = resolved

        def _run() -> dict:
            if not self._ensure_table_exists(schema_name, table_name):
                return self._empty(field=field, issue_type="missing")
            col = self._column_type(schema_name, table_name, field)
            if col is None:
                return self._empty(field=field, issue_type="missing")

            ident = _quote_ident(field)
            table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'
            # 安全说明：table_ref 和 ident 经由 _quote_ident() 转义双引号后安全嵌入；
            # _SAMPLE_LIMIT 为模块级整数常量，无注入风险。其余部分均为静态 SQL。
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

            # 安全说明：同上，标识符已转义，LIMIT 为常量。
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

    async def outlier_iqr_count(self, datasource_id: UUID | str, field: str) -> dict:
        """通过 IQR（四分位距）法检测数值字段中的异常值。"""
        resolved = await self._resolve(datasource_id)
        if resolved is None:
            return self._empty(field=field, issue_type="outlier_iqr")
        schema_name, table_name = resolved

        def _run() -> dict:
            if not self._ensure_table_exists(schema_name, table_name):
                return self._empty(field=field, issue_type="outlier_iqr")
            col = self._column_type(schema_name, table_name, field)
            if col is None or not self._is_numeric_type(col):
                return self._empty(field=field, issue_type="outlier_iqr")

            ident = _quote_ident(field)
            table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'

            # 安全说明：标识符（ident/table_ref）已通过 _quote_ident() 转义，
            # _SAMPLE_LIMIT 为整数常量，CTE 中无外部动态值拼接。
            sql = (
                f"WITH quantiles AS ("
                f"  SELECT "
                f"    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY {ident}) AS q1, "
                f"    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY {ident}) AS q3, "
                f"    COUNT(*) AS total_count "
                f"  FROM {table_ref} WHERE {ident} IS NOT NULL"
                f"), iqr_bounds AS ("
                f"  SELECT q1, q3, (q3 - q1) * 1.5 AS iqr FROM quantiles"
                f"), flagged AS ("
                f"  SELECT {ident} AS v FROM {table_ref}, iqr_bounds "
                f"  WHERE {ident} IS NOT NULL AND iqr_bounds.iqr IS NOT NULL "
                f"    AND ({ident} < iqr_bounds.q1 - iqr_bounds.iqr OR {ident} > iqr_bounds.q3 + iqr_bounds.iqr)"
                f") "
                f"SELECT (SELECT COUNT(*) FROM flagged) AS outlier_count, "
                f"       (SELECT total_count FROM quantiles) AS total_count, "
                f"       (SELECT ARRAY_AGG(v) FROM (SELECT v FROM flagged LIMIT {_SAMPLE_LIMIT})) AS samples"
            )
            rows = duckdb_client.fetchall(sql)
            if not rows:
                return self._empty(field=field, issue_type="outlier_iqr")
            outlier_n = int(rows[0][0]) if rows[0][0] is not None else 0
            total = int(rows[0][1]) if rows[0][1] else 0
            samples_raw = rows[0][2]
            sample_values = list(samples_raw) if isinstance(samples_raw, (list, tuple)) else []
            percentage = (outlier_n / total * 100.0) if total > 0 else 0.0
            return {
                "field": field,
                "issue_type": "outlier_iqr",
                "count": outlier_n,
                "percentage": round(percentage, 4),
                "sample_values": sample_values,
            }

        return await asyncio.to_thread(_run)

    async def type_inconsistency_count(self, datasource_id: UUID | str, field: str) -> dict:
        """检测字段中类型不一致的行。"""
        resolved = await self._resolve(datasource_id)
        if resolved is None:
            return self._empty(field=field, issue_type="type_mismatch")
        schema_name, table_name = resolved

        def _run() -> dict:
            if not self._ensure_table_exists(schema_name, table_name):
                return self._empty(field=field, issue_type="type_mismatch")
            col = self._column_type(schema_name, table_name, field)
            if col is None:
                return self._empty(field=field, issue_type="type_mismatch")

            ident = _quote_ident(field)
            table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'

            # 安全说明：标识符已转义，SQL 主体为静态逻辑，无外部变量拼接。
            if self._is_numeric_type(col):
                sql = (
                    f"SELECT "
                    f"  SUM(CASE WHEN TRY_CAST(CAST({ident} AS VARCHAR) AS DOUBLE) IS NULL "
                    f"       AND {ident} IS NOT NULL THEN 1 ELSE 0 END) AS bad_count, "
                    f"  COUNT(*) AS total_count "
                    f"FROM {table_ref}"
                )
            elif self._is_string_type(col):
                sql = (
                    f"SELECT "
                    f"  SUM(CASE WHEN LENGTH(CAST({ident} AS VARCHAR)) > 500 "
                    f"       OR LENGTH(CAST({ident} AS VARCHAR)) = 0 "
                    f"       THEN 1 ELSE 0 END) AS bad_count, "
                    f"  COUNT(*) AS total_count "
                    f"FROM {table_ref}"
                )
            else:
                return self._empty(field=field, issue_type="type_mismatch")

            rows = duckdb_client.fetchall(sql)
            if not rows:
                return self._empty(field=field, issue_type="type_mismatch")
            bad_n = int(rows[0][0]) if rows[0][0] is not None else 0
            total = int(rows[0][1]) if rows[0][1] else 0
            percentage = (bad_n / total * 100.0) if total > 0 else 0.0

            # 安全说明：同上，标识符已转义，LIMIT 为常量。
            sample_sql = (
                f"SELECT CAST({ident} AS VARCHAR) FROM {table_ref} "
                f"WHERE {ident} IS NOT NULL AND length(CAST({ident} AS VARCHAR)) > 0 "
                f"LIMIT {_SAMPLE_LIMIT}"
            )
            sample_rows = duckdb_client.fetchall(sample_sql)
            sample_values = [r[0] for r in sample_rows if r[0] is not None]

            return {
                "field": field,
                "issue_type": "type_mismatch",
                "count": bad_n,
                "percentage": round(percentage, 4),
                "sample_values": sample_values,
            }

        return await asyncio.to_thread(_run)

    async def outlier_count(self, datasource_id: UUID | str, field: str) -> dict:
        """通过 Z-Score（3σ）法检测数值字段中的异常值。"""
        resolved = await self._resolve(datasource_id)
        if resolved is None:
            return self._empty(field=field, issue_type="outlier")
        schema_name, table_name = resolved

        def _run() -> dict:
            if not self._ensure_table_exists(schema_name, table_name):
                return self._empty(field=field, issue_type="outlier")
            col = self._column_type(schema_name, table_name, field)
            if col is None or not self._is_numeric_type(col):
                return self._empty(field=field, issue_type="outlier")

            ident = _quote_ident(field)
            table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'
            # 安全说明：标识符已转义，_SAMPLE_LIMIT 为常量，CTE 中无动态值拼接。
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
                return self._empty(field=field, issue_type="outlier")
            outlier_n = int(rows[0][0]) if rows[0][0] is not None else 0
            total = int(rows[0][1]) if rows[0][1] else 0
            samples_raw = rows[0][2]
            sample_values = list(samples_raw) if isinstance(samples_raw, (list, tuple)) else []
            percentage = (outlier_n / total * 100.0) if total > 0 else 0.0
            return {
                "field": field,
                "issue_type": "outlier",
                "count": outlier_n,
                "percentage": round(percentage, 4),
                "sample_values": sample_values,
            }

        return await asyncio.to_thread(_run)

    async def dup_row_count(self, datasource_id: UUID | str) -> dict:
        """检测全表中的完全重复行数量。"""
        resolved = await self._resolve(datasource_id)
        if resolved is None:
            return self._empty(field=None, issue_type="duplicate")
        schema_name, table_name = resolved

        def _run() -> dict:
            if not self._ensure_table_exists(schema_name, table_name):
                return self._empty(field=None, issue_type="duplicate")

            table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'
            # 安全说明：table_ref 已转义，SQL 主体为静态逻辑，无外部变量拼接。
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
                # 安全说明：table_ref 已转义，SQL 主体为静态逻辑。
                sql_simple = (
                    f"SELECT COUNT(*) - COUNT(DISTINCT *) AS dup_count, COUNT(*) AS total_count FROM {table_ref}"
                )
                rows = duckdb_client.fetchall(sql_simple)

            if not rows:
                return self._empty(field=None, issue_type="duplicate")
            dup_n = int(rows[0][0]) if rows[0][0] is not None else 0
            total = int(rows[0][1]) if rows[0][1] else 0
            percentage = (dup_n / total * 100.0) if total > 0 else 0.0

            # 安全说明：标识符已转义，LIMIT 为常量。
            sample_sql = (
                f"WITH d AS (SELECT *, COUNT(*) OVER () AS _cnt FROM {table_ref}) "
                f"SELECT * FROM d WHERE _cnt > 1 LIMIT {_SAMPLE_LIMIT}"
            )
            try:
                sample_rows = duckdb_client.fetchall(sample_sql)
            except Exception:
                sample_rows = []
            sample_values = [
                dict(row._mapping) if hasattr(row, "_mapping") else list(row)
                for row in sample_rows
            ]

            return {
                "field": None,
                "issue_type": "duplicate",
                "count": dup_n,
                "percentage": round(percentage, 4),
                "sample_values": sample_values,
            }

        return await asyncio.to_thread(_run)

    async def format_issue_count(self, datasource_id: UUID | str, field: str) -> dict:
        """检测字符串字段中不符合日期格式（YYYY-MM-DD）的行。"""
        resolved = await self._resolve(datasource_id)
        if resolved is None:
            return self._empty(field=field, issue_type="format")
        schema_name, table_name = resolved

        def _run() -> dict:
            if not self._ensure_table_exists(schema_name, table_name):
                return self._empty(field=field, issue_type="format")
            col = self._column_type(schema_name, table_name, field)
            if col is None or not self._is_string_type(col):
                return self._empty(field=field, issue_type="format")

            ident = _quote_ident(field)
            table_ref = f'{_quote_ident(schema_name)}.{_quote_ident(table_name)}'

            # 安全说明：标识符已转义，LIMIT 为常量，正则表达式为字面量，无动态值拼接。
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
                return self._empty(field=field, issue_type="format")
            bad_n = int(rows[0][0]) if rows[0][0] is not None else 0
            total = int(rows[0][1]) if rows[0][1] else 0
            samples_raw = rows[0][2]
            sample_values = list(samples_raw) if isinstance(samples_raw, (list, tuple)) else []
            percentage = (bad_n / total * 100.0) if total > 0 else 0.0
            return {
                "field": field,
                "issue_type": "format",
                "count": bad_n,
                "percentage": round(percentage, 4),
                "sample_values": sample_values,
            }

        return await asyncio.to_thread(_run)


# ── 兼容旧 API：模块级函数（保持向后兼容） ─────────────────────────────
# 旧代码可能直接调用 `data_quality.null_count(datasource_id, field, db)`，
# 下面的函数会内部创建 Repository 调用新 Service。

async def null_count(datasource_id: UUID | str, field: str, db) -> dict:
    """向后兼容：模块级函数（自动注入 Repository）。"""
    from app.repositories.datasource_schema_repository import (
        SQLAlchemyDataSourceSchemaRepository,
    )
    service = DataQualityService(SQLAlchemyDataSourceSchemaRepository(db))
    return await service.null_count(datasource_id, field)


async def outlier_iqr_count(datasource_id: UUID | str, field: str, db) -> dict:
    """向后兼容：模块级函数。"""
    from app.repositories.datasource_schema_repository import (
        SQLAlchemyDataSourceSchemaRepository,
    )
    service = DataQualityService(SQLAlchemyDataSourceSchemaRepository(db))
    return await service.outlier_iqr_count(datasource_id, field)


async def outlier_count(datasource_id: UUID | str, field: str, db) -> dict:
    """向后兼容：模块级函数。"""
    from app.repositories.datasource_schema_repository import (
        SQLAlchemyDataSourceSchemaRepository,
    )
    service = DataQualityService(SQLAlchemyDataSourceSchemaRepository(db))
    return await service.outlier_count(datasource_id, field)


async def type_inconsistency_count(datasource_id: UUID | str, field: str, db) -> dict:
    """向后兼容：模块级函数。"""
    from app.repositories.datasource_schema_repository import (
        SQLAlchemyDataSourceSchemaRepository,
    )
    service = DataQualityService(SQLAlchemyDataSourceSchemaRepository(db))
    return await service.type_inconsistency_count(datasource_id, field)


async def dup_row_count(datasource_id: UUID | str, db) -> dict:
    """向后兼容：模块级函数。"""
    from app.repositories.datasource_schema_repository import (
        SQLAlchemyDataSourceSchemaRepository,
    )
    service = DataQualityService(SQLAlchemyDataSourceSchemaRepository(db))
    return await service.dup_row_count(datasource_id)


async def format_issue_count(datasource_id: UUID | str, field: str, db) -> dict:
    """向后兼容：模块级函数。"""
    from app.repositories.datasource_schema_repository import (
        SQLAlchemyDataSourceSchemaRepository,
    )
    service = DataQualityService(SQLAlchemyDataSourceSchemaRepository(db))
    return await service.format_issue_count(datasource_id, field)
