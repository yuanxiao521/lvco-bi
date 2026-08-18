import asyncio
from datetime import date, datetime
from decimal import Decimal

import structlog
from app.connectors.base import BaseDataSourceConnector

logger = structlog.get_logger("mysql_connector")


def _json_safe(value):
    """Convert Python date/datetime/Decimal to JSON-serializable types."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _infer_category(col_name: str, data_type: str) -> str:
    lower_name = col_name.lower()
    if data_type.upper() in ("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
                              "HUGEINT", "SMALLINT", "TINYINT", "INT2", "INT4", "INT8", "MONEY",
                              "INT", "MEDIUMINT"):
        return "measure"
    if data_type.upper() in ("DATE", "TIMESTAMP", "DATETIME", "TIMESTAMPTZ"):
        return "time"
    if any(kw in lower_name for kw in ("id", "key", "code", "uuid")):
        return "key"
    return "dimension"


class MySQLConnector(BaseDataSourceConnector):
    async def test_connection(self, conn_info: dict) -> dict:
        """Test MySQL connection. Returns {success, row_count, tables, error}."""
        try:
            import mysql.connector
            conn = await asyncio.to_thread(
                mysql.connector.connect,
                host=conn_info.get("host", "localhost"),
                port=int(conn_info.get("port", 3306)),
                user=conn_info["user"],
                password=conn_info["password"],
                database=conn_info["database"],
                connect_timeout=10,
            )
            cursor = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(
                cursor.execute,
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_type = 'BASE TABLE' ORDER BY table_name",
                (conn_info["database"],),
            )
            tables = await asyncio.to_thread(cursor.fetchall)
            table_names = [t[0] for t in tables]
            await asyncio.to_thread(cursor.close)
            await asyncio.to_thread(conn.close)
            logger.info("mysql_test_success", host=conn_info.get("host"), database=conn_info.get("database"))
            return {"success": True, "row_count": len(table_names), "tables": table_names}
        except ImportError:
            logger.warning("mysql_driver_missing")
            return {"success": False, "error": "mysql-connector-python 未安装。请运行: pip install mysql-connector-python"}
        except Exception as e:
            logger.warning("mysql_test_failed", error=str(e))
            return {"success": False, "error": str(e)[:200]}

    def get_attach_sql(self, conn_info: dict, schema_name: str) -> str:
        """Generate DuckDB ATTACH SQL for MySQL."""
        host = conn_info.get("host", "localhost")
        port = conn_info.get("port", 3306)
        user = conn_info.get("user", "root")
        password = conn_info.get("password", "")
        database = conn_info.get("database", "")
        # URL-encode password to handle special characters
        from urllib.parse import quote_plus
        encoded_pw = quote_plus(password) if password else ""
        return f"ATTACH 'mysql://{user}:{encoded_pw}@{host}:{port}/{database}' AS \"{schema_name}\" (TYPE mysql)"

    def load_to_duckdb(self, client, file_path: str, schema_name: str) -> str:
        raise NotImplementedError("MySQL connector does not support file-based loading")

    def get_schema_meta(self, client, schema_name: str, table_name: str) -> dict:
        """Extract schema metadata from an attached MySQL database via DuckDB."""
        db_name = table_name.split(".")[0] if "." in table_name else table_name
        full_table = f'"{schema_name}"."{db_name}"."{table_name}"' if "." not in table_name else f'"{schema_name}"."{table_name}"'

        # Try DuckDB information_schema first
        try:
            rows = client.fetchall(
                f'SELECT column_name, data_type FROM "{schema_name}".information_schema.columns '
                f"WHERE table_name = ?",
                [table_name.split(".")[-1] if "." in table_name else table_name],
            )
        except Exception:
            rows = []

        if not rows:
            try:
                rows = client.fetchall(f"DESCRIBE {full_table}")
                rows = [(r[0], r[1]) for r in rows] if rows else []
            except Exception:
                rows = []

        fields = []
        for col_name, data_type in rows:
            if col_name == "column_name" and data_type == "column_type":
                continue
            category = _infer_category(col_name, data_type.upper() if data_type else "VARCHAR")
            sample_values = self._get_sample_values(client, schema_name, full_table, col_name)
            fields.append({
                "name": col_name,
                "data_type": data_type.upper() if data_type else "VARCHAR",
                "nullable": True,
                "sample": [_json_safe(v) for v in sample_values],
                "category": category,
                "display_name": col_name,
            })
        return {"fields": fields}

    def get_row_count(self, client, schema_name: str, table_name: str) -> int:
        """Get row count from an attached MySQL database via DuckDB."""
        db_name = table_name.split(".")[0] if "." in table_name else table_name
        full_table = f'"{schema_name}"."{db_name}"."{table_name}"' if "." not in table_name else f'"{schema_name}"."{table_name}"'
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
        try:
            result = client.fetchall(
                f'SELECT DISTINCT "{col_name}" FROM {full_table} '
                f'WHERE "{col_name}" IS NOT NULL LIMIT 3'
            )
            return [row[0] for row in result]
        except Exception:
            return []


mysql_connector = MySQLConnector()
