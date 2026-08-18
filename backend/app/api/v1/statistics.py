"""统计分析 API"""
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource, SourceType
from app.models.user import User
from app.schemas import CamelModel, SuccessResponse

log = logging.getLogger("lvco.statistics")

statistics_router = APIRouter(prefix="/statistics", tags=["统计分析"])


def _json_safe(value):
    """Convert Python date/datetime/Decimal to JSON-serializable types."""
    from datetime import date, datetime
    from decimal import Decimal
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _resolve_table_ref(ds: DataSource, user_id: UUID) -> tuple[str, str]:
    """解析数据源对应的 DuckDB 表引用 (schema_name, table_ref)。

    不同数据源类型:
    - CSV/Excel:  ``"{schema_name}"."data"``
    - PostgreSQL:  ``"{schema_name}".public."{table_name}"``
    - MySQL:       ``"{schema_name}"."{table_name}"``  (无 public schema)
    """
    schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name)

    if ds.source_type in (SourceType.postgresql, SourceType.mysql):
        table_name = "data"
        if isinstance(ds.schema_meta, dict):
            table_name = ds.schema_meta.get("table_name") or "data"
        if ds.source_type == SourceType.postgresql:
            table_ref = f'"{schema_name}".public."{table_name}"'
        else:
            table_ref = f'"{schema_name}"."{table_name}"'
    else:
        table_ref = f'"{schema_name}"."data"'

    return schema_name, table_ref


def _ensure_external_attached(ds: DataSource, schema_name: str) -> None:
    """确保外部数据源(PostgreSQL/MySQL)已 ATTACH 到 DuckDB。

    先尝试查询判断是否已连接，避免重复 ATTACH 耗时。
    """
    from app.utils.crypto import decrypt_value, get_encryption_key
    from app.connectors.postgres_connector import postgres_connector

    # 先检查是否已 ATTACH——快速查询 1 行即可判断
    try:
        duckdb_client.fetchall(f'SELECT 1 FROM "{schema_name}".information_schema.tables LIMIT 1')
        return  # 已连接，直接返回
    except Exception:
        pass

    # 未连接，DETACH 清理残留后重新 ATTACH
    try:
        duckdb_client.execute(f'DETACH "{schema_name}"')
    except Exception:
        pass

    conn_info = dict(ds.connection_config) if ds.connection_config else {}
    key = get_encryption_key()
    if key and conn_info.get("password"):
        try:
            conn_info["password"] = decrypt_value(conn_info["password"], key)
        except Exception:
            pass
    conn_info["host"] = conn_info.get("host", "localhost")
    conn_info["port"] = conn_info.get("port", 5432)
    conn_info["user"] = conn_info.get("username", "postgres")
    conn_info["database"] = conn_info.get("db_name", "")

    try:
        if ds.source_type == SourceType.mysql:
            from app.connectors.mysql_connector import mysql_connector
            attach_sql = mysql_connector.get_attach_sql(conn_info, schema_name)
        else:
            attach_sql = postgres_connector.get_attach_sql(conn_info, schema_name)
        duckdb_client.execute(attach_sql)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "ATTACH_FAILED", "message": f"外部数据源连接失败: {str(e)[:200]}"},
        ) from e


class DescribeRequest(CamelModel):
    datasource_id: UUID


class CorrelationRequest(CamelModel):
    datasource_id: UUID
    fields: list[str] | None = None  # None = all measure fields


@statistics_router.post("/describe")
async def describe_statistics(
    body: DescribeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """描述性统计：count/mean/std/min/max/percentiles/null_rate"""
    from sqlalchemy import select

    # Look up datasource by id, verify ownership
    result = await db.execute(
        select(DataSource).where(DataSource.id == body.datasource_id, DataSource.user_id == current_user.id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="数据源不存在")

    schema_meta = ds.schema_meta or {}
    fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
    measure_fields = [f for f in fields if f.get("category") == "measure"]

    # 如果 schema_meta 中的 measure 字段不足 2 个,可能 category 推断有误,
    # 直接从 DuckDB information_schema 重新拉一次数值字段
    if len(measure_fields) < 2:
        try:
            schema_name_db, table_ref_db = _resolve_table_ref(ds, current_user.id)
            if ds.source_type in (SourceType.postgresql, SourceType.mysql):
                try:
                    _ensure_external_attached(ds, schema_name_db)
                except HTTPException:
                    raise
                except Exception:
                    pass  # ATTACH 失败时跳过补全
            # 解析 schema 和表名（处理 "schema".public."table" 格式）
            schema_part = table_ref_db.split(".")[0].strip('"')
            table_part = table_ref_db.split('"')[-2] if '"' in table_ref_db else "data"
            info_rows = await asyncio.to_thread(
                duckdb_client.fetchall,
                f'SELECT column_name, data_type FROM "{schema_part}".information_schema.columns '
                f"WHERE table_name = ?",
                [table_part],
            )
            if info_rows:
                extra_measures = []
                for col_name, data_type in info_rows:
                    if not col_name or not data_type:
                        continue
                    if col_name in {f["name"] for f in measure_fields}:
                        continue
                    upper_dt = data_type.upper()
                    if upper_dt in ("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL",
                                     "NUMERIC", "REAL", "HUGEINT", "SMALLINT", "TINYINT",
                                     "INT2", "INT4", "INT8", "MONEY"):
                        extra_measures.append({
                            "name": col_name,
                            "category": "measure",
                            "data_type": upper_dt,
                        })
                if extra_measures:
                    measure_fields = measure_fields + extra_measures
        except Exception:
            # fallback 失败时维持原 measure_fields
            pass

    # 过滤掉非数值类型的字段（schema_meta 中可能错误地将字符串字段标记为 measure）
    NUMERIC_TYPES = {
        "BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
        "HUGEINT", "SMALLINT", "TINYINT", "INT2", "INT4", "INT8", "MONEY",
    }
    measure_fields = [
        f for f in measure_fields
        if f.get("data_type", "").upper() in NUMERIC_TYPES
        or f.get("dataType", "").upper() in NUMERIC_TYPES
    ]

    if not measure_fields:
        return SuccessResponse(data={"fields": [], "statistics": [], "failed_fields": []})

    schema_name, table_ref = _resolve_table_ref(ds, current_user.id)

    if ds.source_type in (SourceType.postgresql, SourceType.mysql):
        try:
            _ensure_external_attached(ds, schema_name)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={"code": "ATTACH_FAILED", "message": f"外部数据源连接失败: {str(e)[:200]}"},
            ) from e

    def _safe_round(val, digits=2):
        try:
            return round(float(val), digits) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _describe_field(name: str) -> dict | None:
        """在独立线程里对单个字段跑描述性统计；遇到 DuckDB 内部错误返回 None。

        把 PERCENTILE_CONT 拆成单独子查询，避免某个分位数失败导致整条字段失败。
        """
        # 基础统计（count/mean/std/min/max/null）
        base_sql = (
            f'SELECT '
            f'COUNT("{name}") as cnt, '
            f'AVG("{name}") as mean, '
            f'STDDEV("{name}") as std, '
            f'MIN("{name}") as mn, '
            f'MAX("{name}") as mx, '
            f'SUM(CASE WHEN "{name}" IS NULL THEN 1 ELSE 0 END) as null_cnt '
            f'FROM {table_ref}'
        )
        try:
            row = duckdb_client.execute(base_sql).fetchone()
        except Exception as e:
            log.warning("基础统计 %s 失败: %s", name, str(e)[:120])
            return None
        if not row:
            return None
        count_val = int(row[0] or 0)
        null_count_val = int(row[5] or 0)

        # 分位数：单独跑，失败不影响其他统计
        p25 = p50 = p75 = None
        for pct, label in [(0.25, 'p25'), (0.5, 'p50'), (0.75, 'p75')]:
            try:
                pct_sql = (
                    f'SELECT PERCENTILE_CONT({pct}) WITHIN GROUP (ORDER BY "{name}") '
                    f'FROM {table_ref}'
                )
                pr = duckdb_client.execute(pct_sql).fetchone()
                if label == 'p25':
                    p25 = _safe_round(pr[0] if pr else None)
                elif label == 'p50':
                    p50 = _safe_round(pr[0] if pr else None)
                else:
                    p75 = _safe_round(pr[0] if pr else None)
            except Exception as e:
                # DuckDB 内部错误常见于 PERCENTILE_CONT，这里静默跳过单个分位数
                log.debug("分位数 %s@%s 跳过: %s", name, pct, str(e)[:80])

        return {
            "field": name,
            "count": count_val,
            "mean": _safe_round(row[1]),
            "std": _safe_round(row[2]),
            "min": _safe_round(row[3]),
            "p25": p25,
            "p50": p50,
            "p75": p75,
            "max": _safe_round(row[4]),
            "null_count": null_count_val,
            "null_rate": round(null_count_val / count_val, 4) if count_val > 0 else 1.0,
        }

    stats = []
    failed_fields: list[str] = []
    for field_info in measure_fields:
        field_name = field_info["name"]
        try:
            res = await asyncio.to_thread(_describe_field, field_name)
        except Exception as e:
            log.warning("统计字段 %s 失败(将跳过): %s", field_name, str(e)[:150])
            failed_fields.append(field_name)
            continue
        if res is None:
            failed_fields.append(field_name)
            continue
        stats.append(res)

    return SuccessResponse(data={"fields": [f["name"] for f in measure_fields], "statistics": stats, "failed_fields": failed_fields})


@statistics_router.post("/correlation")
async def correlation_analysis(
    body: CorrelationRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """相关性矩阵：两两字段的 Pearson CORR"""
    from sqlalchemy import select

    result = await db.execute(
        select(DataSource).where(DataSource.id == body.datasource_id, DataSource.user_id == current_user.id)
    )
    ds = result.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="数据源不存在")

    schema_meta = ds.schema_meta or {}
    all_fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
    measure_fields = [f for f in all_fields if f.get("category") == "measure"]

    # Determine which fields to include
    if body.fields:
        valid_names = {f["name"] for f in measure_fields}
        selected = [f for f in body.fields if f in valid_names]
    else:
        selected = [f["name"] for f in measure_fields]

    if len(selected) < 2:
        return SuccessResponse(data={"fields": selected, "matrix": [[1.0]] if selected else []})

    schema_name, table_ref = _resolve_table_ref(ds, current_user.id)

    if ds.source_type in (SourceType.postgresql, SourceType.mysql):
        try:
            _ensure_external_attached(ds, schema_name)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={"code": "ATTACH_FAILED", "message": f"外部数据源连接失败: {str(e)[:200]}"},
            ) from e

    # Build NxN matrix
    n = len(selected)
    matrix: list[list[float]] = [[0.0] * n for _ in range(n)]

    def _safe_float(value) -> float:
        """把 DuckDB 返回的值转 float：None/NaN/Inf 一律变成 0.0，避免 JSON 序列化失败。"""
        if value is None:
            return 0.0
        try:
            v = float(value)
            if v != v or v in (float("inf"), float("-inf")):  # NaN/Inf check
                return 0.0
            return v
        except (TypeError, ValueError):
            return 0.0

    def _compute_corr(field_a: str, field_b: str) -> float:
        """在独立线程里计算两个字段的 Pearson 相关系数，避免 DuckDB 内部错误吞掉对角线。"""
        sql = f'SELECT CORR(CAST("{field_a}" AS DOUBLE), CAST("{field_b}" AS DOUBLE)) FROM {table_ref}'
        row = duckdb_client.execute(sql).fetchone()
        return _safe_float(row[0] if row else None)

    for i in range(n):
        matrix[i][i] = 1.0
        for j in range(i):
            try:
                val = await asyncio.to_thread(_compute_corr, selected[i], selected[j])
                val = round(val, 4)
                matrix[i][j] = val
                matrix[j][i] = val  # symmetric
            except Exception as e:
                log.warning("相关性 %s vs %s 失败(置 0): %s", selected[i], selected[j], str(e)[:150])
                matrix[i][j] = 0.0
                matrix[j][i] = 0.0
                # 注意：不要重置 matrix[i][i] = 1.0（对角线保持 1.0）

    # 二次校验：把所有 NaN/Inf 强制归零，确保 JSON 一定可序列化
    cleaned: list[list[float]] = []
    for row in matrix:
        cleaned.append([_safe_float(v) for v in row])

    return SuccessResponse(data={"fields": selected, "matrix": cleaned})


class RankingRequest(CamelModel):
    datasource_id: str
    metric: dict  # {"field": "field_name", "agg": "SUM"}
    dimension: str
    limit: int = 10
    order: str = "desc"  # "asc" or "desc"


@statistics_router.post("/ranking")
async def ranking_analysis(
    body: RankingRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """Ranking analysis: Top N / Bottom N by a metric grouped by dimension."""
    from sqlalchemy import select

    ds = await db.execute(select(DataSource).where(DataSource.id == body.datasource_id, DataSource.user_id == current_user.id))
    datasource = ds.scalar_one_or_none()
    if not datasource:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "数据源不存在"})

    metric_field = body.metric.get("field", "")
    metric_agg = body.metric.get("agg", "SUM").upper()
    from app.services.query_engine import ALLOWED_AGGREGATIONS as ALLOWED_AGGS
    if metric_agg not in ALLOWED_AGGS:
        raise HTTPException(status_code=400, detail={"code": "INVALID_AGG", "message": f"不支持的聚合: {metric_agg}"})

    dim = body.dimension
    direction = "DESC" if body.order.lower() == "desc" else "ASC"

    schema_name, table_ref = _resolve_table_ref(datasource, current_user.id)

    if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
        _ensure_external_attached(datasource, schema_name)

    if metric_agg == "COUNT_DISTINCT":
        agg_expr = f'COUNT(DISTINCT "{metric_field}")'
    elif metric_agg == "STDDEV":
        agg_expr = f'STDDEV("{metric_field}")'
    elif metric_agg == "MEDIAN":
        agg_expr = f'MEDIAN("{metric_field}")'
    else:
        agg_expr = f'{metric_agg}("{metric_field}")'

    sql = f'SELECT "{dim}" AS label, {agg_expr} AS value FROM {table_ref} GROUP BY "{dim}" ORDER BY value {direction} LIMIT {body.limit}'

    try:
        rows_raw = await asyncio.to_thread(duckdb_client.fetchall, sql, None)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "SQL_FAILED", "message": f"排名查询失败: {str(e)[:200]}"},
        ) from e
    data = [{"label": str(row[0]), "value": float(row[1]) if row[1] is not None else 0} for row in rows_raw]

    return SuccessResponse(data={
        "dimension": dim,
        "metric": {"field": metric_field, "agg": metric_agg},
        "data": data,
        "order": body.order,
    })


class SummaryRequest(CamelModel):
    datasource_id: UUID


@statistics_router.post("/summary")
async def datasource_summary(
    body: SummaryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """Get data source summary: row count, column count, distinct keys, date range."""
    from sqlalchemy import select

    ds = await db.execute(select(DataSource).where(DataSource.id == body.datasource_id, DataSource.user_id == current_user.id))
    datasource = ds.scalar_one_or_none()
    if not datasource:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "数据源不存在"})

    schema_name, table_ref = _resolve_table_ref(datasource, current_user.id)

    if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
        _ensure_external_attached(datasource, schema_name)

    try:
        count_rows = await asyncio.to_thread(duckdb_client.fetchall, f'SELECT COUNT(*) FROM {table_ref}', None)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "SQL_FAILED", "message": f"汇总查询失败: {str(e)[:200]}"},
        ) from e
    total_rows = count_rows[0][0] if count_rows else 0

    fields = (datasource.schema_meta.get("fields") or []) if isinstance(datasource.schema_meta, dict) else []
    total_columns = len(fields)

    date_cols = [f for f in fields if isinstance(f, dict) and f.get("data_type", "").upper() in ("DATE", "TIMESTAMP", "DATETIME", "TIMESTAMPTZ")]
    date_range = None
    if date_cols:
        date_col = date_cols[0]["name"]
        try:
            min_r = await asyncio.to_thread(duckdb_client.fetchall, f'SELECT MIN("{date_col}"), MAX("{date_col}") FROM {table_ref}', None)
            if min_r and min_r[0][0] is not None:
                date_range = {"min": str(min_r[0][0]), "max": str(min_r[0][1])}
        except Exception:
            pass

    dim_fields = [f for f in fields if isinstance(f, dict) and f.get("category") == "dimension"]
    distinct_keys = 0
    if dim_fields:
        dim_col = dim_fields[0]["name"]
        try:
            dk = await asyncio.to_thread(duckdb_client.fetchall, f'SELECT COUNT(DISTINCT "{dim_col}") FROM {table_ref}', None)
            distinct_keys = dk[0][0] if dk else 0
        except Exception:
            pass

    return SuccessResponse(data={
        "total_rows": total_rows,
        "total_columns": total_columns,
        "distinct_keys": distinct_keys,
        "date_range": date_range,
    })


class ComparisonRequest(CamelModel):
    datasource_id: str
    date_field: str          # e.g. "order_date"
    metric_field: str        # e.g. "total_amount"
    metric_agg: str = "SUM"  # SUM, AVG, COUNT, etc.
    period: str = "month"    # "month", "quarter", "year"
    compare_type: str = "mom"  # "mom" (环比) or "yoy" (同比)
    dimension: str | None = None  # optional group dimension


@statistics_router.post("/comparison")
async def comparison_analysis(
    body: ComparisonRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """Comparison analysis: Year-over-Year or Month-over-Month growth rate.

    Uses DuckDB LAG window function to compute period-over-period comparisons.
    """
    from sqlalchemy import select

    # Validate datasource
    ds = await db.execute(
        select(DataSource).where(
            DataSource.id == body.datasource_id,
            DataSource.user_id == current_user.id,
        )
    )
    datasource = ds.scalar_one_or_none()
    if not datasource:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    # Validate aggregation
    from app.services.query_engine import ALLOWED_AGGREGATIONS
    agg = body.metric_agg.upper()
    if agg not in ALLOWED_AGGREGATIONS:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_AGG", "message": f"不支持的聚合: {agg}"},
        )

    schema_name, table_ref = _resolve_table_ref(datasource, current_user.id)

    if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
        _ensure_external_attached(datasource, schema_name)

    # Build aggregation expression
    if agg == "COUNT_DISTINCT":
        agg_expr = f'COUNT(DISTINCT "{body.metric_field}")'
    elif agg == "STDDEV":
        agg_expr = f'STDDEV("{body.metric_field}")'
    elif agg == "MEDIAN":
        agg_expr = f'MEDIAN("{body.metric_field}")'
    else:
        agg_expr = f'{agg}("{body.metric_field}")'

    # Determine DATE_TRUNC period and LAG offset
    if body.period == "month":
        trunc = "month"
        lag_offset = 12 if body.compare_type == "yoy" else 1
    elif body.period == "quarter":
        trunc = "quarter"
        lag_offset = 4 if body.compare_type == "yoy" else 1
    else:  # year
        trunc = "year"
        lag_offset = 1  # both yoy and mom use 1 for yearly

    date_field = body.date_field
    dim = body.dimension

    if dim:
        # With dimension grouping
        sql = f"""
        WITH aggregated AS (
            SELECT 
                DATE_TRUNC('{trunc}', "{date_field}") AS period,
                "{dim}" AS dimension_label,
                {agg_expr} AS value
            FROM {table_ref}
            WHERE "{date_field}" IS NOT NULL
            GROUP BY 1, 2
        ),
        with_prev AS (
            SELECT 
                period,
                dimension_label,
                value,
                LAG(value, {lag_offset}) OVER (
                    PARTITION BY dimension_label ORDER BY period
                ) AS prev_value
            FROM aggregated
        )
        SELECT 
            strftime(period, '%Y-%m-%d') AS period_str,
            dimension_label,
            ROUND(value, 2) AS value,
            ROUND(prev_value, 2) AS prev_value,
            CASE 
                WHEN prev_value IS NOT NULL AND prev_value != 0 
                THEN ROUND((value - prev_value) / prev_value * 100, 2)
                ELSE NULL 
            END AS change_pct
        FROM with_prev
        ORDER BY period DESC, dimension_label
        """
    else:
        # Without dimension grouping
        sql = f"""
        WITH aggregated AS (
            SELECT 
                DATE_TRUNC('{trunc}', "{date_field}") AS period,
                {agg_expr} AS value
            FROM {table_ref}
            WHERE "{date_field}" IS NOT NULL
            GROUP BY 1
        ),
        with_prev AS (
            SELECT 
                period,
                value,
                LAG(value, {lag_offset}) OVER (ORDER BY period) AS prev_value
            FROM aggregated
        )
        SELECT 
            strftime(period, '%Y-%m-%d') AS period_str,
            ROUND(value, 2) AS value,
            ROUND(prev_value, 2) AS prev_value,
            CASE 
                WHEN prev_value IS NOT NULL AND prev_value != 0 
                THEN ROUND((value - prev_value) / prev_value * 100, 2)
                ELSE NULL 
            END AS change_pct
        FROM with_prev
        ORDER BY period DESC
        """

    try:
        rows_raw = await asyncio.to_thread(duckdb_client.fetchall, sql, None)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "SQL_FAILED", "message": f"对比查询失败: {str(e)[:200]}"},
        ) from e

    if dim:
        data = []
        for row in rows_raw:
            data.append({
                "period": str(row[0]),
                "dimension": str(row[1]) if row[1] else "",
                "value": float(row[2]) if row[2] is not None else 0,
                "prev_value": float(row[3]) if row[3] is not None else None,
                "change_pct": float(row[4]) if row[4] is not None else None,
            })
    else:
        data = []
        for row in rows_raw:
            data.append({
                "period": str(row[0]),
                "value": float(row[1]) if row[1] is not None else 0,
                "prev_value": float(row[2]) if row[2] is not None else None,
                "change_pct": float(row[3]) if row[3] is not None else None,
            })

    return SuccessResponse(data={
        "metric": {"field": body.metric_field, "agg": agg},
        "compare_type": body.compare_type,
        "period": body.period,
        "data": data,
    })


class PreviewRequest(CamelModel):
    datasource_id: str
    limit: int = 5


@statistics_router.post("/preview")
async def data_preview(
    body: PreviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """快速预览数据前 N 行"""
    from sqlalchemy import select

    ds = await db.execute(
        select(DataSource).where(DataSource.id == body.datasource_id, DataSource.user_id == current_user.id)
    )
    datasource = ds.scalar_one_or_none()
    if not datasource:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "数据源不存在"})

    schema_name, table_ref = _resolve_table_ref(datasource, current_user.id)
    if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
        _ensure_external_attached(datasource, schema_name)

    try:
        rows = await asyncio.to_thread(
            duckdb_client.fetchall,
            f"SELECT * FROM {table_ref} LIMIT {min(body.limit, 20)}",
            None,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "SQL_FAILED", "message": f"预览查询失败: {str(e)[:200]}"},
        ) from e

    # Get column names
    cols = []
    if isinstance(datasource.schema_meta, dict):
        cols = [f["name"] for f in (datasource.schema_meta.get("fields") or []) if isinstance(f, dict)]

    data = []
    for row in rows:
        if cols and len(cols) == len(row):
            data.append(dict(zip(cols, [_json_safe(v) for v in row])))
        else:
            data.append([_json_safe(v) for v in row])

    return SuccessResponse(data={"columns": cols or [f"col_{i}" for i in range(len(rows[0]) if rows else 0)], "rows": data, "total_previewed": len(data)})
