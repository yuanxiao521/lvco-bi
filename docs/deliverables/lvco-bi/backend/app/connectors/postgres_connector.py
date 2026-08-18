import asyncio
import structlog
from datetime import date, datetime
from decimal import Decimal
from app.connectors.base import BaseDataSourceConnector

logger = structlog.get_logger("postgres_connector")


def _json_safe(value):
    """Convert Python date/datetime/Decimal to JSON-serializable types."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _infer_category(col_name: str, data_type: str) -> str:
    lower_name = col_name.lower()
    # 优先级：先按 data_type 判断(最准确)，再用列名兜底
    if data_type in ("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
                      "HUGEINT", "SMALLINT", "TINYINT", "INT2", "INT4", "INT8", "MONEY"):
        return "measure"
    if data_type in ("DATE", "TIMESTAMP", "TIMESTAMPTZ"):
        return "time"
    if any(kw in lower_name for kw in ("id", "key", "code", "uuid")):
        return "key"
    return "dimension"


class PostgresConnector(BaseDataSourceConnector):
    async def test_connection(self, conn_info: dict) -> dict:
        """Test PostgreSQL connection."""
        try:
            import psycopg2
            conn = await asyncio.to_thread(
                psycopg2.connect,
                host=conn_info.get("host", "localhost"),
                port=int(conn_info.get("port", 5432)),
                user=conn_info["user"],
                password=conn_info["password"],
                dbname=conn_info["database"],
                connect_timeout=10,
            )
            cursor = await asyncio.to_thread(conn.cursor)
            await asyncio.to_thread(cursor.execute, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name")
            tables = await asyncio.to_thread(cursor.fetchall)
            table_names = [t[0] for t in tables]
            await asyncio.to_thread(cursor.close)
            await asyncio.to_thread(conn.close)
            logger.info("postgres_test_success", host=conn_info.get("host"), database=conn_info.get("database"))
            return {"success": True, "row_count": len(table_names), "tables": table_names}
        except ImportError:
            logger.warning("postgres_driver_missing")
            return {"success": False, "error": "psycopg2 未安装。请运行: pip install psycopg2-binary"}
        except Exception as e:
            logger.warning("postgres_test_failed", error=str(e))
            return {"success": False, "error": str(e)[:200]}

    def get_attach_sql(self, conn_info: dict, schema_name: str) -> str:
        """Generate DuckDB ATTACH SQL for a PostgreSQL database."""
        host = conn_info.get("host", "localhost")
        port = conn_info.get("port", 5432)
        user = conn_info.get("user", conn_info.get("username", "postgres"))
        password = conn_info.get("password", "")
        database = conn_info.get("database", conn_info.get("db_name", ""))
        return (
            f"ATTACH 'host={host} port={port} user={user} password={password}"
            f" dbname={database}' AS \"{schema_name}\" (TYPE postgres, READ_ONLY)"
        )

    def load_to_duckdb(self, client, file_path: str, schema_name: str) -> str:
        """Not used for PostgreSQL; attach happens in sync_datasource service."""
        raise NotImplementedError("Use ATTACH instead of load")

    def get_schema_meta(self, client, schema_name: str, table_name: str) -> dict:
        """Extract schema metadata from an attached PostgreSQL database."""
        # Use pg_catalog directly from the attached DB, not DuckDB's information_schema
        full_table = f'"{schema_name}".public."{table_name}"'
        rows = client.fetchall(
            f'SELECT column_name, data_type FROM "{schema_name}".information_schema.columns '
            f"WHERE table_schema = 'public' AND table_name = ?",
            [table_name],
        )
        if not rows:
            # Fallback: use DESCRIBE
            rows = client.fetchall(f"DESCRIBE {full_table}")
            # DESCRIBE returns: column_name, column_type, null, key, default, extra
            rows = [(r[0], r[1]) for r in rows] if rows else []

        fields = []
        for col_name, data_type in rows:
            if col_name == "column_name" and data_type == "column_type":
                continue  # skip header row from DESCRIBE
            category = _infer_category(col_name, data_type.upper() if data_type else "VARCHAR")
            sample_values = self._get_sample_values(client, schema_name, table_name, col_name)
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
        return [r[0] for r in rows]

    def _get_sample_values(self, client, schema_name: str, table_name: str, col_name: str) -> list:
        result = client.fetchall(
            f'SELECT DISTINCT "{col_name}" FROM "{schema_name}".public."{table_name}" '
            f'WHERE "{col_name}" IS NOT NULL LIMIT 3'
        )
        return [row[0] for row in result]


postgres_connector = PostgresConnector()
