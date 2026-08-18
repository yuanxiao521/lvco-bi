import asyncio
import structlog
from app.connectors.base import BaseDataSourceConnector

logger = structlog.get_logger("mysql_connector")

class MySQLConnector(BaseDataSourceConnector):
    async def test_connection(self, conn_info: dict) -> dict:
        """Test MySQL connection. Returns {success: bool, row_count?: int, error?: str}."""
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
            await asyncio.to_thread(cursor.execute, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s", (conn_info["database"],))
            row = await asyncio.to_thread(cursor.fetchone)
            table_count = row[0] if row else 0
            await asyncio.to_thread(cursor.close)
            await asyncio.to_thread(conn.close)
            logger.info("mysql_test_success", host=conn_info.get("host"), database=conn_info.get("database"))
            return {"success": True, "row_count": table_count}
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
        return f"ATTACH 'mysql://{user}:{password}@{host}:{port}/{database}' AS {schema_name} (TYPE mysql)"

    def load_to_duckdb(self, client, file_path: str, schema_name: str) -> str:
        raise NotImplementedError("MySQL connector does not support file-based loading")

    def get_schema_meta(self, client, schema_name: str, table_name: str) -> dict:
        raise NotImplementedError("MySQL connector does not support file-based schema extraction")

    def get_row_count(self, client, schema_name: str, table_name: str) -> int:
        raise NotImplementedError("MySQL connector does not support file-based row counting")


mysql_connector = MySQLConnector()
