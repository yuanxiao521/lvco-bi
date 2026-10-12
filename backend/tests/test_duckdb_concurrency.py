# -*- coding: utf-8 -*-
"""DuckDB 客户端并发改造测试（方案 A 游标并发 + 方案 B read_only/写窗口）。

对照旧实现：单连接 + 全局 _conn_lock 把所有查询（含只读 SELECT）串行化。
新实现：
  A. 同实例多游标并发读（DuckDB MVCC：多读并发、写由引擎层串行化）；
  B. DUCKDB_READ_ONLY=true 时只读打开（可与其它 read_only 进程并行），
     写操作必须经 write_window() 独占升级。
"""
import os
import subprocess
import sys
import threading
import time

import duckdb
import pytest

from app.core import duckdb_client as dc_mod
from app.core.duckdb_client import DuckDBClient


def _reset_singleton(tmp_path, monkeypatch, read_only: bool = False):
    """把全局单例指到临时目录，测试间完全隔离。"""
    monkeypatch.setattr(dc_mod.settings, "DUCKDB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dc_mod.settings, "DUCKDB_READ_ONLY", read_only)
    DuckDBClient._instance = None
    DuckDBClient._conn = None
    DuckDBClient._attached_cache.clear()
    DuckDBClient._cursor_pool.clear()
    return DuckDBClient()


@pytest.fixture()
def fresh_client(tmp_path, monkeypatch):
    """隔离单例 + 预置 1000 行数据表。"""
    client = _reset_singleton(tmp_path, monkeypatch)
    client.execute("CREATE TABLE t (id INTEGER, v DOUBLE)")
    client.execute("INSERT INTO t SELECT i, i * 1.5 FROM range(1000) AS s(i)")
    yield client
    client.close()
    DuckDBClient._instance = None
    DuckDBClient._conn = None


# ---------- 方案 A：功能正确性 ----------

def test_execute_chained_fetchone(fresh_client):
    """execute() 返回值可链式消费（statistics.py 的既有用法不能破）。"""
    row = fresh_client.execute("SELECT count(*), sum(v) FROM t").fetchone()
    assert row[0] == 1000
    assert abs(row[1] - 1000 * 999 / 2 * 1.5) < 1e-6


def test_fetchall_fetchdf_correct(fresh_client):
    rows = fresh_client.fetchall("SELECT id FROM t WHERE id < 3 ORDER BY id")
    assert [r[0] for r in rows] == [0, 1, 2]
    df = fresh_client.fetchdf("SELECT count(*) AS n FROM t")
    assert int(df["n"][0]) == 1000


def test_concurrent_mixed_read_write(fresh_client):
    """并发混合负载：6 线程读 + 2 线程写，无异常且最终数据一致（写由引擎层串行化）。"""
    errors: list[Exception] = []

    def reader():
        try:
            for _ in range(50):
                n = fresh_client.fetchall("SELECT count(*) FROM t")[0][0]
                assert n >= 1000
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def writer(start: int):
        try:
            for i in range(200):
                fresh_client.execute(
                    "INSERT INTO t VALUES (?, ?)", [start + i, float(start + i)]
                )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads += [threading.Thread(target=writer, args=(10_000,)), threading.Thread(target=writer, args=(20_000,))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"并发中出现异常: {errors[:3]}"
    assert fresh_client.fetchall("SELECT count(*) FROM t")[0][0] == 1000 + 400


def test_concurrent_attach_distinct_schemas(fresh_client, tmp_path):
    """多线程并发 ATTACH 不同 schema，各自成功且互不串台（_attach_lock 原子性）。"""
    # 先准备两个可被 ATTACH 的 duckdb 文件
    for name in ("db_a", "db_b"):
        sub = duckdb.connect(str(tmp_path / f"{name}.duckdb"))
        sub.execute("CREATE TABLE mark(v INTEGER)")
        sub.execute(f"INSERT INTO mark VALUES ({1 if name.endswith('a') else 2})")
        sub.close()

    def attach_one(schema: str, path: str, expect: int, errs: list):
        try:
            fresh_client.ensure_attached(
                schema, f"ATTACH '{path}' AS {schema}", force=True
            )
            got = fresh_client.fetchall(f"SELECT v FROM {schema}.mark")[0][0]
            assert got == expect
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    errs: list[Exception] = []
    threads = [
        threading.Thread(target=attach_one, args=("db_a", str(tmp_path / "db_a.duckdb"), 1, errs)),
        threading.Thread(target=attach_one, args=("db_b", str(tmp_path / "db_b.duckdb"), 2, errs)),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errs, f"ATTACH 并发异常: {errs}"


# ---------- 方案 A：并行收益 ----------

@pytest.mark.skipif((os.cpu_count() or 2) < 4, reason="并行收益断言需要 >=4 核")
def test_small_queries_throughput(fresh_client):
    """大量小查询：并发吞吐应明显优于串行（旧实现下两者相等——全被锁串行）。"""
    small = "SELECT count(*) FROM range(2000000) AS s(i)"
    n = 48
    t0 = time.perf_counter()
    for _ in range(n):
        fresh_client.fetchall(small)
    t_serial = time.perf_counter() - t0

    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(_run_batch(fresh_client, small, n // 4)))
        for _ in range(4)
    ]
    t0 = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t_parallel = time.perf_counter() - t0

    assert all(results), "并发批次有失败"
    assert t_parallel < t_serial * 0.8, (
        f"并发未见收益: serial={t_serial:.2f}s parallel={t_parallel:.2f}s"
    )


def _run_batch(client, sql: str, times: int) -> float:
    t0 = time.perf_counter()
    for _ in range(times):
        client.fetchall(sql)
    return time.perf_counter() - t0


@pytest.mark.skipif((os.cpu_count() or 2) < 4, reason="并行收益断言需要 >=4 核")
def test_short_queries_not_blocked_by_heavy_query(fresh_client):
    """旧实现的标志性痛点：一个重查询进行时，小查询必须等锁。新实现下小查询应当穿插完成。"""
    heavy = "SELECT count(*) FROM (SELECT 1 FROM range(12000) a CROSS JOIN range(12000) b) x"

    t0 = time.perf_counter()
    fresh_client.fetchall(heavy)
    t_heavy = time.perf_counter() - t0

    done = threading.Event()
    threading.Thread(target=lambda: (fresh_client.fetchall(heavy), done.set())).start()

    t0 = time.perf_counter()
    for _ in range(5):
        fresh_client.fetchall("SELECT count(*) FROM range(1000000) AS s(i)")
    t_short = time.perf_counter() - t0

    assert done.wait(timeout=30), "重查询未在时限内完成"
    assert t_short < t_heavy * 0.6, (
        f"小查询疑似被重查询阻塞: t_short={t_short:.3f}s t_heavy={t_heavy:.3f}s"
    )


# ---------- 方案 B：read_only + 写窗口 ----------

def test_readonly_mode_and_write_window(tmp_path, monkeypatch):
    """RO 模式下只读可用、写被拒；write_window 内可写；窗口外恢复只读。"""
    # 先以读写模式建库建表
    client = _reset_singleton(tmp_path, monkeypatch, read_only=False)
    client.execute("CREATE TABLE base (id INTEGER)")
    client.execute("INSERT INTO base VALUES (1)")
    client.close()
    DuckDBClient._instance = None
    DuckDBClient._conn = None
    DuckDBClient._cursor_pool.clear()

    # 切只读
    monkeypatch.setattr(dc_mod.settings, "DUCKDB_READ_ONLY", True)
    ro_client = DuckDBClient()
    assert ro_client.fetchall("SELECT count(*) FROM base")[0][0] == 1

    # 只读下写被拒
    with pytest.raises(duckdb.Error):
        ro_client.execute("CREATE TABLE should_fail (id INTEGER)")

    # 写窗口内可写
    with ro_client.write_window() as wconn:
        wconn.execute("CREATE TABLE written_in_window (id INTEGER)")
        wconn.execute("INSERT INTO written_in_window VALUES (42)")
    assert ro_client.fetchall("SELECT count(*) FROM written_in_window")[0][0] == 1

    # 窗口外恢复只读
    with pytest.raises(duckdb.Error):
        ro_client.execute("CREATE TABLE should_fail_again (id INTEGER)")

    ro_client.close()
    DuckDBClient._instance = None
    DuckDBClient._conn = None


def test_multi_process_readonly_parallel(tmp_path, monkeypatch):
    """跨进程只读并行（方案 B 的核心收益）：两个子进程同时 RO 打开同一文件查询都成功；
    且 RO 常驻时写模式打开被拒（写独占语义保持）。"""
    client = _reset_singleton(tmp_path, monkeypatch, read_only=False)
    client.execute("CREATE TABLE t (id INTEGER)")
    client.execute("INSERT INTO t VALUES (1),(2),(3)")
    client.close()
    DuckDBClient._instance = None
    DuckDBClient._conn = None

    db = str(tmp_path / "lvco_bi.duckdb")
    code = (
        "import duckdb, sys\n"
        f"c = duckdb.connect(r'{db}', read_only=True)\n"
        "r = c.execute('SELECT sum(id) FROM t').fetchone()\n"
        "print('OK', r[0])\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate() for p in procs]
    for i, (out, err) in enumerate(outs):
        assert procs[i].returncode == 0, f"子进程 {i} 失败: {err}"
        assert "OK 6" in out, f"子进程 {i} 输出异常: {out}"

    # 反向验证：一个 RO 连接常驻时，写模式打开必须被拒（独占语义未丢）
    holder = duckdb.connect(db, read_only=True)
    try:
        with pytest.raises(duckdb.Error):
            duckdb.connect(db, read_only=False)
    finally:
        holder.close()
