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

    def _init_conn(self, conn: duckdb.DuckDBPyConnection) -> None:
        """对新连接做统一初始化：内存上限 + 扩展加载。"""
        conn.execute(f"SET memory_limit='{settings.DUCKDB_MEMORY_LIMIT}'")
        # 装载 spatial 扩展（Excel 上传需要）
        try:
            conn.execute("INSTALL spatial;")
            conn.execute("LOAD spatial;")
        except duckdb.Error as e:
            log.warning("spatial extension unavailable, Excel upload will fail: %s", e)
        # 装载 postgres_scanner 扩展（PostgreSQL ATTACH、洞察扫描、定时执行需要）
        try:
            conn.execute("INSTALL postgres_scanner;")
            conn.execute("LOAD postgres_scanner;")
        except duckdb.Error as e:
            log.error("postgres_scanner extension unavailable, PostgreSQL features will fail: %s", e)
        # 装载 mysql 扩展（MySQL ATTACH 需要）
        try:
            conn.execute("INSTALL mysql;")
            conn.execute("LOAD mysql;")
        except duckdb.Error as e:
            log.error("mysql extension unavailable, MySQL ATTACH will fail: %s", e)

    def _connect(self, db_path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(str(db_path), read_only=read_only)
        self._init_conn(conn)
        return conn

    def _connect_with_readonly_fallback(self, db_path: Path) -> duckdb.DuckDBPyConnection:
        """连接数据库；若文件被其他进程独占（File is already open），给出清晰可读的占用提示。

        DuckDB 多进程规则：只要有一个进程以读写打开文件，其他进程无论读写都打不开；
        且 Windows 上被锁时连文件复制都不允许。因此这里不做静默降级（拷贝副本会因
        WinError 32 失败），而是抛出一个中文可读错误，引导用户停止占用方（后端服务或
        另一个评测进程）后再重试 —— 评测/分析应当串行使用同一个 DuckDB 数据文件。
        """
        try:
            return self._connect(db_path)
        except duckdb.Error as e:
            if "already open" not in str(e).lower():
                raise
            raise duckdb.Error(
                "lvco_bi.duckdb 正被其他进程独占（File is already open）。"
                "DuckDB 数据库文件同一时刻只允许一个进程以读写方式打开，"
                "且 Windows 上被锁期间连只读副本也无法复制。"
                "请先停止占用方（如正在运行的后端服务，或另一个评测进程）后再重试。"
                f" 原始错误: {e}"
            ) from e

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    data_dir = Path(settings.DUCKDB_DATA_DIR)
                    data_dir.mkdir(parents=True, exist_ok=True)
                    db_path = data_dir / "lvco_bi.duckdb"
                    self._conn = self._connect_with_readonly_fallback(db_path)
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
