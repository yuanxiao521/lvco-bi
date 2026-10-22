import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb

from app.config import settings

log = logging.getLogger("lvco.duckdb")

# 游标池上限：借出池只用于「方法内部闭环」的查询（fetchall/fetchdf）；
# execute() 成功后游标交给调用方链式消费（.fetchone()/.df()），不归还，由 GC 回收。
_CURSOR_POOL_LIMIT = 8


class DuckDBClient:
    _instance: "DuckDBClient | None" = None
    _lock: threading.Lock = threading.Lock()
    _conn_lock: threading.Lock = threading.Lock()
    _conn: duckdb.DuckDBPyConnection | None = None
    # 已 ATTACH 的外部数据库 schema 缓存：首次 attach 后复用，避免每次查询重复 DETACH+ATTACH。
    # 进程重启后为空 → 首次查询自然重新 attach（跨重启旧连接失效问题由"重建"覆盖）。
    _attached_cache: set[str] = set()
    # 方案 A：读游标池。同一实例的多个游标可并发执行（DuckDB MVCC：多读并发、写全局串行，
    # 并发控制交给引擎层）——取代旧实现"一把 _conn_lock 把所有查询（含只读）串行化"。
    _cursor_pool: list[duckdb.DuckDBPyConnection] = []
    _pool_lock: threading.Lock = threading.Lock()
    # ATTACH/DETACH 序列锁：保证"清理残留 → ATTACH → 记缓存"三步对同名 schema 原子。
    _attach_lock: threading.Lock = threading.Lock()
    # 方案 B：写窗口锁 —— read_only 模式下写操作（导入/删源）必须独占升级连接。
    _write_lock: threading.Lock = threading.Lock()

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

        方案 B 起此约束反转：DUCKDB_READ_ONLY=true 时本进程以只读打开，可与其它
        read_only 进程（评测/多副本）并行；写操作必须经 write_window() 独占升级。
        """
        try:
            ro = bool(getattr(settings, "DUCKDB_READ_ONLY", False))
            conn = self._connect(db_path, read_only=ro)
            if ro:
                # RO 模式：本进程成为"读者"——注册心跳 + 启动写意图协调线程
                _coordination.register_reader()
            return conn
        except duckdb.Error as e:
            if "already open" not in str(e).lower():
                raise
            mode = "只读" if getattr(settings, "DUCKDB_READ_ONLY", False) else "读写"
            raise duckdb.Error(
                "lvco_bi.duckdb 正被其他进程独占（File is already open）。"
                f"本进程尝试以{mode}模式打开失败。DuckDB 规则：读写连接独占文件；"
                "多个只读连接可并行，但只要存在一个读写连接，其它进程（含只读）都无法打开。"
                "请先停止占用方（如正在运行的后端服务、正在导入的写窗口，或另一个评测进程）后再重试。"
                f" 原始错误: {e}"
            ) from e

    def _db_path(self) -> Path:
        data_dir = Path(settings.DUCKDB_DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "lvco_bi.duckdb"

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    self._conn = self._connect_with_readonly_fallback(self._db_path())
        return self._conn

    # ---------- 方案 A：游标借出池 ----------

    def _acquire_cursor(self) -> duckdb.DuckDBPyConnection:
        """借一个游标：优先复用池内空闲游标，否则基于主连接新建（同实例，共享 catalog）。"""
        with self._pool_lock:
            if self._cursor_pool:
                return self._cursor_pool.pop()
        return self._get_connection().cursor()

    def _release_cursor(self, cur: duckdb.DuckDBPyConnection) -> None:
        with self._pool_lock:
            if len(self._cursor_pool) < _CURSOR_POOL_LIMIT:
                self._cursor_pool.append(cur)

    def _drain_cursors(self) -> None:
        """清空游标池（写窗口切换连接模式前必须调用，否则旧游标指向已关闭的连接）。"""
        with self._pool_lock:
            self._cursor_pool.clear()

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._get_connection()

    def execute(self, query: str, params: list | None = None) -> duckdb.DuckDBPyConnection:
        """执行一条 SQL 并返回游标（调用方可链式 .fetchone()/.fetchall()/.df()）。

        并发语义（方案 A）：游标相互独立，SELECT 可并行（引擎内 MVCC 多读），
        写语句由 DuckDB 引擎层串行化——不再需要应用层全局锁把读也排队。
        成功路径不归还游标：返回值归调用方消费，用完由 GC 回收（cursor 创建成本极低）。
        """
        cur = self._acquire_cursor()
        try:
            if params:
                return cur.execute(query, params)
            return cur.execute(query)
        except Exception:
            # 失败路径游标状态未知（事务可能中断），直接丢弃不回池，由 GC 回收。
            raise

    def fetchall(self, query: str, params: list | None = None) -> list[tuple]:
        cur = self._acquire_cursor()
        try:
            if params:
                result = cur.execute(query, params)
            else:
                result = cur.execute(query)
            return result.fetchall()
        finally:
            self._release_cursor(cur)

    def fetchdf(self, query: str, params: list | None = None):
        cur = self._acquire_cursor()
        try:
            if params:
                result = cur.execute(query, params)
            else:
                result = cur.execute(query)
            return result.fetchdf()
        finally:
            self._release_cursor(cur)

    # ---------- 方案 B：写窗口 ----------

    @contextmanager
    def write_window(self):
        """写窗口：read_only 模式下执行写操作的独占升级通道。

        DUCKDB_READ_ONLY=false（默认）时本进程已是读写连接，直接透传、零开销。
        RO 模式下：进入时关闭当前连接与游标池 → 以读写模式重开（此时进程内独占文件）→
        yield → finally 关闭写连接、清池、置 None（下次访问按配置惰性重建只读连接）。
        进程内由 _write_lock 串行；跨进程仍受 DuckDB 文件锁约束——写窗口期间其它
        read_only 进程（评测/另一副本）会暂时打不开文件，属预期行为（导入低频）。
        """
        if not getattr(settings, "DUCKDB_READ_ONLY", False):
            yield self._get_connection()
            return
        with self._write_lock:
            # 本进程先下线（关连接 + 删心跳），并广播写意图让**其它**读者也下线
            with self._conn_lock:
                if self._conn is not None:
                    try:
                        self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._conn = None
                self._drain_cursors()
            _coordination.unregister_reader()
            intent_token = f"{os.getpid()}-{time.time()}"
            _coordination.set_intent(intent_token)
            try:
                # 等其它读者下线（它们的协调线程看到意图后会自行关连接）
                deadline = time.monotonic() + _INTENT_WAIT_TIMEOUT
                while True:
                    others = _coordination.foreign_readers()
                    if not others:
                        break
                    if time.monotonic() > deadline:
                        raise duckdb.Error(
                            "等待其它只读进程释放 DuckDB 超时（"
                            f"{_INTENT_WAIT_TIMEOUT}s，仍有 {len(others)} 个读者）。"
                            "请稍后重试导入，或检查是否有异常占用 DuckDB 的进程。"
                        )
                    time.sleep(0.3)
                self._conn = self._connect(self._db_path(), read_only=False)
                yield self._conn
            finally:
                with self._conn_lock:
                    if self._conn is not None:
                        try:
                            self._conn.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self._conn = None
                    self._drain_cursors()
                # 撤销意图：读者线程会重新打开连接并注册心跳
                _coordination.clear_intent()

    def ensure_attached(self, schema_name: str, attach_sql: str, *, force: bool = False) -> bool:
        """确保外部数据库已 ATTACH 到 DuckDB；已附加则直接复用，避免重复建连。

        首次对该 schema 调用时执行 ATTACH；后续调用命中进程内缓存直接跳过。
        进程重启后缓存为空，首个请求自然重新 ATTACH（覆盖跨重启旧连接失效场景）。
        若连接信息发生变化（如数据源同步、密码轮换），传 `force=True` 强制重建。

        Args:
            schema_name: ATTACH 使用的 schema（database）名。
            attach_sql: 由对应连接器生成的完整 ATTACH SQL。
            force: 置 True 时跳过缓存，DETACH 旧连接后重新 ATTACH。

        Returns:
            True 表示本次实际执行了 ATTACH（新建连接）；False 表示复用了已有连接。
        """
        if not force and schema_name in self._attached_cache:
            return False
        with self._attach_lock:
            if not force and schema_name in self._attached_cache:
                return False
            # 先清理同名残留（schema 不存在时 DETACH 会报错，忽略）
            try:
                self.execute(f'DETACH "{schema_name}"')
            except Exception:
                pass
            self.execute(attach_sql)
            self._attached_cache.add(schema_name)
        return True

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
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._drain_cursors()



# ── 方案 B（升级版）：跨进程 DuckDB 写窗口协调 ──────────────────────────────
#
# 背景（实测）：DuckDB 文件锁的规则是「多个只读进程可并行；存在任一读写连接时，
# 其它进程（含只读）都打不开」。API 与 worker 各持一个只读连接后，上传/删源的
# 写窗口会永远打不开文件——除非**所有读者主动下线**。本协调器用 Redis 完成
# "写方广播意图 → 读者自行下线 → 写方独占 → 读者回来" 的闭环：
#
#   写方 write_window():  SET duckdb:write-intent
#                         轮询等 duckdb:reader:* 全部消失（读者线程看到意图后自行关连接）
#                         打开读写连接执行 → 关闭 → DEL intent
#   读方协调线程:         每 0.5s 看 intent：有 → 关连接 + 删自己的 reader 心跳；
#                         无 → 连接在则续期心跳（TTL 5s，进程崩溃自动过期）
#
# 只在 DUCKDB_READ_ONLY=true 时启用；Redis 不可用时全部静默跳过——退化为
# "写窗口只管本进程" 的旧行为（单进程部署依旧正确）。
_INTENT_KEY = "duckdb:write-intent"
_READER_PREFIX = "duckdb:reader:"
_READER_TTL = 5            # 心跳 TTL（秒）：进程崩溃后自动过期
_COORD_INTERVAL = 0.5      # 协调线程轮询间隔（秒）
_INTENT_WAIT_TIMEOUT = 15  # 写方等读者下线的上限（秒）；超时给出明确报错而不是挂死


class _DuckWriteCoordination:
    """跨进程写窗口协调（Redis 心跳 + 意图广播；Redis 掉线时全部退化为空操作）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pid = str(os.getpid())
        self._registered = False

    def _repo(self):
        try:
            from app.core.cache import get_cache_repository

            return get_cache_repository()
        except Exception:  # noqa: BLE001
            return None

    # ── 读方 ──────────────────────────────────────────────────────────────

    def register_reader(self) -> None:
        """本进程持有了只读连接：注册心跳并惰性启动协调线程。"""
        self._registered = True
        self._ensure_thread()

    def unregister_reader(self) -> None:
        """本进程连接已关闭（写窗口进入/显式 close）：下线心跳。"""
        self._registered = False
        repo = self._repo()
        if repo is None:
            return
        try:
            repo.delete_shared(_READER_PREFIX + self._pid)
        except Exception:  # noqa: BLE001
            pass

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._coordination_loop, name="duckdb-ro-coordination", daemon=True
            )
            self._thread.start()

    def _coordination_loop(self) -> None:
        """读方协调线程：看意图下线 / 续期心跳。任何异常都不致命（下轮重来）。"""
        while True:
            time.sleep(_COORD_INTERVAL)
            try:
                repo = self._repo()
                if repo is None:
                    continue
                intent = repo.get(_INTENT_KEY)
                if intent:
                    # 写方要独占：立即关闭本进程连接并下线（下次查询惰性重开）
                    if self._registered:
                        with _client._conn_lock:
                            if _client._conn is not None:
                                try:
                                    _client._conn.close()
                                except Exception:  # noqa: BLE001
                                    pass
                            _client._conn = None
                            _client._drain_cursors()
                        self._registered = False
                        repo.delete_shared(_READER_PREFIX + self._pid)
                    continue
                if self._registered:
                    # 续期心跳（证明本进程仍持连接）
                    repo.set_shared(_READER_PREFIX + self._pid, "1", ttl=_READER_TTL)
            except Exception:  # noqa: BLE001
                continue

    # ── 写方 ──────────────────────────────────────────────────────────────

    def set_intent(self, token: str) -> None:
        repo = self._repo()
        if repo is None:
            return
        try:
            repo.set_shared(_INTENT_KEY, token, ttl=30)
        except Exception:  # noqa: BLE001
            pass

    def clear_intent(self) -> None:
        repo = self._repo()
        if repo is None:
            return
        try:
            repo.delete_shared(_INTENT_KEY)
        except Exception:  # noqa: BLE001
            pass

    def foreign_readers(self) -> list[str]:
        """还在持连接的**其它**读者（自己的心跳不算）。"""
        repo = self._repo()
        if repo is None:
            return []
        try:
            keys = repo.scan_shared_keys(_READER_PREFIX)
            return [k for k in keys if k != _READER_PREFIX + self._pid]
        except Exception:  # noqa: BLE001
            return []


_coordination = _DuckWriteCoordination()


duckdb_client = DuckDBClient()
