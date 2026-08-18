import logging
import re
import threading
from pathlib import Path

import duckdb

from app.config import settings

log = logging.getLogger("lvco.duckdb")


class DuckDBClient:
    _instance: "DuckDBClient | None" = None
    _lock: threading.Lock = threading.Lock()
    _conn_lock: threading.Lock = threading.Lock()
    _conn: duckdb.DuckDBPyConnection | None = None

    def __new__(cls) -> "DuckDBClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    data_dir = Path(settings.DUCKDB_DATA_DIR)
                    data_dir.mkdir(parents=True, exist_ok=True)
                    db_path = data_dir / "lvco_bi.duckdb"
                    self._conn = duckdb.connect(str(db_path))
                    self._conn.execute(f"SET memory_limit='{settings.DUCKDB_MEMORY_LIMIT}'")
                    # 装载 spatial 扩展（Excel 上传需要）
                    try:
                        self._conn.execute("INSTALL spatial;")
                        self._conn.execute("LOAD spatial;")
                    except duckdb.Error as e:
                        log.warning("spatial extension unavailable, Excel upload will fail: %s", e)
                    # 装载 postgres_scanner 扩展（PostgreSQL ATTACH、洞察扫描、定时执行需要）
                    try:
                        self._conn.execute("INSTALL postgres_scanner;")
                        self._conn.execute("LOAD postgres_scanner;")
                    except duckdb.Error as e:
                        log.error("postgres_scanner extension unavailable, PostgreSQL features will fail: %s", e)
        return self._conn

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._get_connection()

    def execute(self, query: str, params: list | None = None) -> duckdb.DuckDBPyConnection:
        conn = self._get_connection()
        with self._conn_lock:
            if params:
                return conn.execute(query, params)
            return conn.execute(query)

    def fetchall(self, query: str, params: list | None = None) -> list[tuple]:
        conn = self._get_connection()
        with self._conn_lock:
            if params:
                result = conn.execute(query, params)
            else:
                result = conn.execute(query)
            return result.fetchall()

    def fetchdf(self, query: str, params: list | None = None):
        conn = self._get_connection()
        with self._conn_lock:
            if params:
                result = conn.execute(query, params)
            else:
                result = conn.execute(query)
            return result.fetchdf()

    def get_schema_name(self, user_id: str | object, datasource_id: str | object,
                        datasource_name: str = "", db_name: str = "") -> str:
        """生成 DuckDB schema 名称。优先用 db_name 保证不同库不串数据。"""
        did = str(datasource_id).replace("-", "")[:8]
        # 优先级：db_name > datasource_name > user_hash
        if db_name:
            safe = re.sub(r'[^a-zA-Z0-9_]', '_', str(db_name))[:20]
            return f"{safe}_{did}"
        if datasource_name:
            safe_name = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fff]', '_', str(datasource_name))[:16]
            return f"{safe_name}_{did}"
        uid = str(user_id).replace("-", "")[:8]
        return f"{uid}_{did}"

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


duckdb_client = DuckDBClient()
