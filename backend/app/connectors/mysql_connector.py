import asyncio

import structlog

from app.config import settings
from app.connectors.attach_sql import build_attach_statements
from app.connectors.base import BaseDataSourceConnector
from app.utils.json_safe import json_safe as _json_safe

logger = structlog.get_logger("mysql_connector")

_MEASURE_TYPES = (
    "BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
    "HUGEINT", "SMALLINT", "TINYINT", "INT2", "INT4", "INT8", "MONEY",
    "INT", "MEDIUMINT", "UINTEGER", "UBIGINT",
)
_TIME_TYPES = ("DATE", "TIMESTAMP", "DATETIME", "TIMESTAMPTZ", "TIME", "TIMETZ")


def _infer_category(col_name: str, data_type: str) -> str:
    lower_name = col_name.lower()
    if data_type in _MEASURE_TYPES:
        return "measure"
    if data_type in _TIME_TYPES:
        return "time"
    if any(kw in lower_name for kw in ("id", "key", "code", "uuid")):
        return "key"
    return "dimension"


def _conn_kwargs(conn_info: dict) -> dict:
    """统一连接参数（兼容 user/username、database/db_name 两套历史命名）。"""
    kwargs = {
        "host": conn_info.get("host", "localhost"),
        "port": int(conn_info.get("port") or 3306),
        "user": conn_info.get("user") or conn_info.get("username") or "root",
        "password": conn_info.get("password", ""),
        "database": conn_info.get("database") or conn_info.get("db_name") or "",
        "connect_timeout": int(settings.DB_CONNECT_TIMEOUT_SECONDS),
    }
    ssl_mode = conn_info.get("sslmode") or conn_info.get("ssl_mode")
    if ssl_mode:
        kwargs["ssl_disabled"] = str(ssl_mode).lower() in ("disable", "disabled", "false")
    return kwargs


class MySQLConnector(BaseDataSourceConnector):
    async def test_connection(self, conn_info: dict) -> dict:
        """Test MySQL connection. Returns {success, row_count, tables, error}."""
        try:
            import mysql.connector
        except ImportError:
            logger.warning("mysql_driver_missing")
            return {"success": False,
                    "error": "缺少 MySQL 驱动（mysql-connector-python），请联系管理员安装后重启服务"}

        try:
            conn = await asyncio.to_thread(mysql.connector.connect, **_conn_kwargs(conn_info))
        except Exception as e:  # noqa: BLE001
            from app.services.datasource_connection import sanitize_error
            msg = sanitize_error(str(e), secrets=[conn_info.get("password")])
            logger.warning("mysql_test_failed", host=conn_info.get("host"), error=msg)
            return {"success": False, "error": msg}

        try:
            cursor = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(
                cursor.execute,
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
                (conn_info.get("database") or conn_info.get("db_name"),),
            )
            tables = await asyncio.to_thread(cursor.fetchall)
            table_names = [t[0] for t in tables]
            await asyncio.to_thread(cursor.close)
            logger.info("mysql_test_success", host=conn_info.get("host"),
                        database=conn_info.get("database") or conn_info.get("db_name"))
            return {"success": True, "row_count": len(table_names), "tables": table_names}
        finally:
            await asyncio.to_thread(conn.close)

    def get_attach_sql(self, conn_info: dict, schema_name: str) -> list[str]:
        """生成 DuckDB 的 [CREATE SECRET, ATTACH] 语句（密码不进连接串）。"""
        return build_attach_statements(conn_info, schema_name, "mysql")

    def load_to_duckdb(self, client, file_path: str, schema_name: str) -> str:
        raise NotImplementedError("MySQL connector does not support file-based loading")

    def get_schema_meta(self, client, schema_name: str, table_name: str) -> dict:
        """Extract schema metadata from an attached MySQL database via DuckDB."""
        full_table = f'"{schema_name}"."{table_name}"'

        # Try DuckDB information_schema first
        try:
            rows = client.fetchall(
                f'SELECT column_name, data_type FROM "{schema_name}".information_schema.columns '
                f"WHERE table_name = ?",
                [table_name.split(".")[-1] if "." in table_name else table_name],
            )
        except Exception:  # noqa: BLE001
            rows = []

        if not rows:
            try:
                rows = client.fetchall(f"DESCRIBE {full_table}")
                rows = [(r[0], r[1]) for r in rows] if rows else []
            except Exception:  # noqa: BLE001
                rows = []

        fields = []
        for col_name, data_type in rows:
            if col_name == "column_name" and data_type == "column_type":
                continue
            try:
                sample_values = self._get_sample_values(client, schema_name, full_table, col_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mysql_sample_values_failed", column=col_name, error=str(exc)[:120])
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
        """Get row count from an attached MySQL database via DuckDB."""
        full_table = f'"{schema_name}"."{table_name}"'
        result = client.fetchall(f"SELECT COUNT(*) FROM {full_table}")
        return result[0][0] if result else 0

    def list_tables(self, client, schema_name: str) -> list[str]:
        """List all tables in the attached MySQL database."""
        rows = client.fetchall(
            f'SELECT table_name FROM "{schema_name}".information_schema.tables '
            f"WHERE table_type = 'BASE TABLE'"
        )
        return [r[0] for r in rows]

    def _get_sample_values(self, client, schema_name: str, full_table: str, col_name: str) -> list:
        result = client.fetchall(
            f'SELECT DISTINCT "{col_name}" FROM {full_table} '
            f'WHERE "{col_name}" IS NOT NULL LIMIT 3'
        )
        return [row[0] for row in result]


mysql_connector = MySQLConnector()
