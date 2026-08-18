"""自动发现 - 扫描数据源 schema，识别可监控的表"""

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger("lvco.insight.discovery")


# 时间字段名/类型关键字
TIME_NAME_PATTERNS = [
    r"date", r"time", r"timestamp", r"datetime",
    r"created", r"updated", r"modified", r"occurred",
    r"月", r"日", r"时间",
]
TIME_TYPE_PATTERNS = [
    "date", "time", "timestamp", "datetime",
]

# 度量字段名/类型关键字
MEASURE_NAME_PATTERNS = [
    r"amount", r"price", r"total", r"sum", r"count", r"qty",
    r"revenue", r"sales", r"cost", r"profit", r"margin",
    r"金额", r"价格", r"数量", r"总额", r"收入", r"成本", r"利润",
]
MEASURE_TYPE_PATTERNS = ["int", "integer", "bigint", "smallint", "decimal", "numeric", "float", "double", "real"]


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count_estimate: int = 0


@dataclass
class DiscoveryCandidate:
    table: str
    time_field: str
    measure_fields: list[str]
    dimension_fields: list[str]
    confidence: float
    rationale: str


def _is_time_name(name: str) -> bool:
    n = name.lower()
    return any(re.search(p, n) for p in TIME_NAME_PATTERNS)


def _is_time_type(data_type: str) -> bool:
    t = data_type.lower()
    return any(p in t for p in TIME_TYPE_PATTERNS)


def _is_measure_name(name: str) -> bool:
    n = name.lower()
    return any(re.search(p, n) for p in MEASURE_NAME_PATTERNS)


def _is_measure_type(data_type: str) -> bool:
    t = data_type.lower()
    return any(p in t for p in MEASURE_TYPE_PATTERNS)


def _score_candidate(
    time_fields: list[str],
    measure_fields: list[str],
    row_count: int,
) -> float:
    """启发式打分：时间字段 + 度量字段 + 行数"""
    score = 0.0
    if time_fields:
        score += 0.4
    if measure_fields:
        score += 0.4
    if row_count >= 30:
        score += 0.1
    if row_count >= 365:
        score += 0.1  # 一年以上数据更好
    return min(score, 1.0)


def _build_rationale(
    table: str,
    time_field: str,
    measure_fields: list[str],
    row_count: int,
) -> str:
    return (
        f"表 `{table}` 含时间字段 `{time_field}` 和 "
        f"{len(measure_fields)} 个度量字段（约 {row_count} 行），适合日报监控。"
    )


def discover_candidates(tables: list[TableInfo]) -> list[DiscoveryCandidate]:
    """从一组表中识别可监控候选"""
    candidates: list[DiscoveryCandidate] = []
    for t in tables:
        time_cols = [c for c in t.columns if _is_time_name(c.name) or _is_time_type(c.data_type)]
        measure_cols = [
            c for c in t.columns
            if (c.name not in {tc.name for tc in time_cols})
            and (_is_measure_name(c.name) or _is_measure_type(c.data_type))
        ]
        dimension_cols = [
            c for c in t.columns
            if c.name not in {tc.name for tc in time_cols}
            and c.name not in {mc.name for mc in measure_cols}
            and not _is_measure_type(c.data_type)
        ]

        if not time_cols or not measure_cols:
            continue
        if t.row_count_estimate < 10:
            continue

        time_field = time_cols[0].name
        confidence = _score_candidate(
            [c.name for c in time_cols],
            [c.name for c in measure_cols],
            t.row_count_estimate,
        )
        candidates.append(DiscoveryCandidate(
        table=t.name,
        time_field=time_field,
        measure_fields=[c.name for c in measure_cols],
        dimension_fields=[c.name for c in dimension_cols],
        confidence=confidence,
        rationale=_build_rationale(
            t.name, time_field,
            [c.name for c in measure_cols],
            t.row_count_estimate,
        ),
    ))
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


# Column names that are typically primary/foreign keys, not measures
_KEY_NAME_SUFFIXES = ("_id", "id")
_KEY_NAME_EXACT = {"id", "uuid", "guid", "pk"}


def _is_key_column(name: str) -> bool:
    """Identify primary/foreign key columns to exclude from measure fields."""
    n = name.lower()
    if n in _KEY_NAME_EXACT:
        return True
    return any(n.endswith(s) for s in _KEY_NAME_SUFFIXES if s != "id" or n != "id")


def _build_table_infos_from_columns(
    tables_cols: dict[str, list[tuple[str, str]]],
    row_counts: dict[str, int],
) -> list[TableInfo]:
    """Build TableInfo list from grouped column data, filtering out key columns."""
    table_infos: list[TableInfo] = []
    for tname, cols in tables_cols.items():
        columns = [
            ColumnInfo(name=c, data_type=d)
            for c, d in cols
            if not _is_key_column(c)
        ]
        # Keep key columns as dimensions? No — exclude them entirely from discovery
        # to avoid "id" being misclassified as a measure by _is_measure_type("int").
        table_infos.append(TableInfo(
            name=tname,
            columns=columns,
            row_count_estimate=row_counts.get(tname, 0),
        ))
    return table_infos


def build_suggestions_from_candidates(
    candidates: list[DiscoveryCandidate],
    table_row_counts: dict[str, int],
    user_id,
    datasource_id,
) -> list:
    """Build InsightSuggestion ORM objects from discovery candidates.

    Returns list of InsightSuggestion (NOT yet added to session).
    Caller is responsible for db.add() + db.commit().
    """
    from app.models.insight_suggestion import InsightSuggestion, SuggestionStatus

    suggestions = []
    for c in candidates:
        suggestion = InsightSuggestion(
            user_id=user_id,
            datasource_id=datasource_id,
            table_name=c.table,
            time_field=c.time_field,
            measure_fields=c.measure_fields,
            dimension_fields=c.dimension_fields,
            suggested_name=f"{c.table} 日报",
            suggested_config={
                "table": c.table,
                "time_field": c.time_field,
                "measures": [{"field": f, "agg": "SUM"} for f in c.measure_fields[:3]],
                "dimensions": c.dimension_fields[:2],
                "filters": [],
                "time_range_days": 30,
            },
            rationale=c.rationale,
            confidence=c.confidence,
            row_count_estimate=table_row_counts.get(c.table, 0),
            update_frequency="medium",
            status=SuggestionStatus.pending,
        )
        suggestions.append(suggestion)
    return suggestions


async def scan_datasource(db, datasource, user_id) -> list:
    """Scan a PostgreSQL datasource's schema, discover monitorable tables,
    and persist InsightSuggestion records.

    Only PostgreSQL is supported (MySQL connector lacks list_tables).
    Returns the list of created InsightSuggestion objects.
    """
    from app.connectors.postgres_connector import postgres_connector
    from app.core.duckdb_client import duckdb_client
    from app.models.datasource import SourceType
    from app.utils.crypto import decrypt_value, get_encryption_key

    if datasource.source_type != SourceType.postgresql:
        logger.warning(
            "scan_datasource skips non-postgresql source",
            extra={"source_type": str(datasource.source_type)},
        )
        return []

    schema_name = duckdb_client.get_schema_name(user_id, datasource.id)
    conn_info = dict(datasource.connection_config or {})

    # Decrypt password
    key = get_encryption_key()
    password = conn_info.get("password", "")
    if key and password:
        conn_info["password"] = decrypt_value(password, key)
    # Map stored keys to connector-expected keys
    conn_info["host"] = conn_info.get("host", "localhost")
    conn_info["port"] = conn_info.get("port", 5432)
    conn_info["user"] = conn_info.get("username", "postgres")
    conn_info["database"] = conn_info.get("db_name", "")

    # ATTACH (DETACH first to ensure fresh connection)
    try:
        duckdb_client.execute(f'DETACH "{schema_name}"')
    except Exception:
        pass  # schema not attached yet, ignore
    attach_sql = postgres_connector.get_attach_sql(conn_info, schema_name)
    duckdb_client.execute(attach_sql)

    # Fetch all columns of all public tables in ONE query (lightweight, no sample values)
    rows = duckdb_client.fetchall(f'''
        SELECT table_name, column_name, data_type
        FROM "{schema_name}".information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    ''')

    # Group columns by table
    tables_cols: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for tname, cname, dtype in rows:
        tables_cols[tname].append((cname, dtype or "varchar"))

    # Estimate row count per table (skip tables that fail)
    row_counts: dict[str, int] = {}
    for tname in tables_cols:
        try:
            row_counts[tname] = postgres_connector.get_row_count(
                duckdb_client, schema_name, tname
            )
        except Exception as e:
            logger.warning("row_count_failed", extra={"table": tname, "error": str(e)})
            row_counts[tname] = 0

    # Build TableInfo list (filtering out key columns)
    table_infos = _build_table_infos_from_columns(tables_cols, row_counts)

    # Heuristic discovery
    candidates = discover_candidates(table_infos)

    # Build + persist InsightSuggestion records
    suggestions = build_suggestions_from_candidates(
        candidates,
        row_counts,
        user_id,
        datasource.id,
    )
    for s in suggestions:
        db.add(s)
    if suggestions:
        await db.commit()
        for s in suggestions:
            await db.refresh(s)

    logger.info(
        "scan_datasource_done",
        extra={
            "datasource_id": str(datasource.id),
            "tables_scanned": len(tables_cols),
            "candidates_found": len(candidates),
        },
    )
    return suggestions
