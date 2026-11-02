import asyncio

import structlog

from app.config import settings
from app.connectors.attach_sql import build_attach_statements
from app.connectors.base import BaseDataSourceConnector
from app.utils.json_safe import json_safe as _json_safe

logger = structlog.get_logger("postgres_connector")

_MEASURE_TYPES = (
    "BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
    "HUGEINT", "SMALLINT", "TINYINT", "INT2", "INT4", "INT8", "MONEY",
    "INT", "MEDIUMINT", "UINTEGER", "UBIGINT",
)
_TIME_TYPES = ("DATE", "TIMESTAMP", "DATETIME", "TIMESTAMPTZ", "TIME", "TIMETZ")


def _infer_category(col_name: str, data_type: str) -> str:
    lower_name = col_name.lower()
    # 优先级：先按 data_type 判断(最准确)，再用列名兜底
    if data_type in _MEASURE_TYPES:
        return "measure"
    if data_type in _TIME_TYPES:
        return "time"
    if any(kw in lower_name for kw in ("id", "key", "code", "uuid")):
        return "key"
    return "dimension"


def _conn_kwargs(conn_info: dict, default_port: int) -> dict:
    """统一连接参数（兼容 user/username、database/db_name 两套历史命名）。"""
    kwargs = {
        "host": conn_info.get("host", "localhost"),
        "port": int(conn_info.get("port") or default_port),
        "user": conn_info.get("user") or conn_info.get("username") or "postgres",
        "password": conn_info.get("password", ""),
        "dbname": conn_info.get("database") or conn_info.get("db_name") or "",
        "connect_timeout": int(settings.DB_CONNECT_TIMEOUT_SECONDS),
    }
    # TLS：sslmode 取值 disable/prefer/require/verify-ca/verify-full（libpq 语义）；
    # verify-* 需要 CA 文件路径（由 datasource_connection.materialize_ca 落地）
    sslmode = str(conn_info.get("sslmode") or conn_info.get("ssl_mode") or "").lower()
    if sslmode:
        kwargs["sslmode"] = sslmode
    ca_path = conn_info.get("ssl_ca_path")
    if ca_path and sslmode in ("verify-ca", "verify-full"):
        kwargs["sslrootcert"] = ca_path
    return kwargs


class PostgresConnector(BaseDataSourceConnector):
    async def test_connection(self, conn_info: dict) -> dict:
        """Test PostgreSQL connection. Returns {success, row_count, tables, error}."""
        try:
            import psycopg2
        except ImportError:
            logger.warning("postgres_driver_missing")
            return {"success": False,
                    "error": "缺少 PostgreSQL 驱动（psycopg2-binary），请联系管理员安装后重启服务"}

        try:
            conn = await asyncio.to_thread(psycopg2.connect, **_conn_kwargs(conn_info, 5432))
        except Exception as e:  # noqa: BLE001
            # 错误信息可能带连接串，统一脱敏后再返回前端
            from app.services.datasource_connection import sanitize_error
            msg = sanitize_error(str(e), secrets=[conn_info.get("password")])
            logger.warning("postgres_test_failed", host=conn_info.get("host"), error=msg)
            return {"success": False, "error": msg}

        try:
            cursor = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(
                cursor.execute,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
            )
            tables = await asyncio.to_thread(cursor.fetchall)
            table_names = [t[0] for t in tables]
            await asyncio.to_thread(cursor.close)
            logger.info("postgres_test_success", host=conn_info.get("host"),
                        database=conn_info.get("database") or conn_info.get("db_name"))
            return {"success": True, "row_count": len(table_names), "tables": table_names}
        finally:
            await asyncio.to_thread(conn.close)

    async def list_tables_direct(self, conn_info: dict) -> list[str]:
        """直接用 psycopg2 列出 public 表，绕过 DuckDB ATTACH。"""
        import psycopg2

        _SYSTEM_PREFIXES = ("pg_", "sql_")
        conn = await asyncio.to_thread(psycopg2.connect, **_conn_kwargs(conn_info, 5432))
        try:
            cursor = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(
                cursor.execute,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name",
            )
            rows = await asyncio.to_thread(cursor.fetchall)
            await asyncio.to_thread(cursor.close)
            tables = [r[0] for r in rows]
            return [t for t in tables if not t.lower().startswith(_SYSTEM_PREFIXES)]
        finally:
            await asyncio.to_thread(conn.close)

    async def get_schema_fields_direct(self, conn_info: dict, table_name: str) -> list[dict]:
        """直接用 psycopg2 获取表的字段信息。"""
        import psycopg2

        conn = await asyncio.to_thread(psycopg2.connect, **_conn_kwargs(conn_info, 5432))
        try:
            cursor = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(
                cursor.execute,
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position",
                (table_name,),
            )
            rows = await asyncio.to_thread(cursor.fetchall)
            await asyncio.to_thread(cursor.close)
            fields = []
            for col_name, data_type in rows:
                category = _infer_category(col_name, data_type.upper() if data_type else "VARCHAR")
                fields.append({
                    "name": col_name,
                    "data_type": data_type.upper() if data_type else "VARCHAR",
                    "nullable": True,
                    "category": category,
                    "display_name": col_name,
                })
            return fields
        finally:
            await asyncio.to_thread(conn.close)

    def get_attach_sql(self, conn_info: dict, schema_name: str) -> list[str]:
        """生成 DuckDB 的 [CREATE SECRET, ATTACH] 语句（密码不进连接串）。"""
        return build_attach_statements(conn_info, schema_name, "postgresql")

    def load_to_duckdb(self, client, file_path: str, schema_name: str) -> str:
        """Not used for PostgreSQL; attach happens in sync_datasource service."""
        raise NotImplementedError("Use ATTACH instead of load")

    def get_schema_meta(self, client, schema_name: str, table_name: str) -> dict:
        """Extract schema metadata from an attached PostgreSQL database.

        每列独立 try/except：单列取样例失败（权限/类型不支持）不影响整表同步。
        """
        full_table = f'"{schema_name}".public."{table_name}"'
        rows = client.fetchall(
            f'SELECT column_name, data_type FROM "{schema_name}".information_schema.columns '
            "WHERE table_schema = 'public' AND table_name = ?",
            [table_name],
        )
        if not rows:
            # Fallback: use DESCRIBE
            rows = client.fetchall(f"DESCRIBE {full_table}")
            rows = [(r[0], r[1]) for r in rows] if rows else []

        fields = []
        for col_name, data_type in rows:
            if col_name == "column_name" and data_type == "column_type":
                continue  # skip header row from DESCRIBE
            try:
                sample_values = self._get_sample_values(client, schema_name, table_name, col_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pg_sample_values_failed", column=col_name, error=str(exc)[:120])
                sample_values = []
            fields.append({
                "name": col_name,
                "data_type": data_type.upper() if data_type else "VARCHAR",
                "nullable": True,
                "sample": [_json_safe(v) for v in sample_values],
                "category": _infer_category(col_name, data_type.upper() if data_type else "VARCHAR"),
                "display_name": col_name,
            })
        return {"fields": fields}

    def get_row_count(self, client, schema_name: str, table_name: str) -> int:
        """Get row count from an attached PostgreSQL database."""
        result = client.fetchall(
            f'SELECT COUNT(*) FROM "{schema_name}".public."{table_name}"'
        )
        return result[0][0] if result else 0

    def list_tables(self, client, schema_name: str) -> list[str]:
        """List all public tables in the attached PostgreSQL database."""
        rows = client.fetchall(
            f'SELECT table_name FROM "{schema_name}".information_schema.tables '
            f"WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        if not rows:
            # Fallback: try PostgreSQL-specific query
            rows = client.fetchall(
                f'SELECT tablename FROM "{schema_name}".pg_catalog.pg_tables '
                f"WHERE schemaname = 'public'"
            )
        # 过滤内置系统表（pg_/sql_ 前缀的为 PostgreSQL 内部表，不应暴露给用户）
        _SYSTEM_PREFIXES = ("pg_", "sql_")
        tables = [r[0] for r in rows]
        return [t for t in tables if not t.lower().startswith(_SYSTEM_PREFIXES)]

    def _get_sample_values(self, client, schema_name: str, table_name: str, col_name: str) -> list:
        return client.fetchall(
            f'SELECT DISTINCT "{col_name}" FROM "{schema_name}".public."{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL LIMIT 3'
        )


postgres_connector = PostgresConnector()
