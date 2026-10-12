# -*- coding: utf-8 -*-
"""DuckDB 查询层并发压测：模拟「多用户同时触发分析」的负载，量化方案 A 收益。

一次「分析」= 12 条查询（模拟 Worker 真实查询模式）：
  1 × heavy  （~2e7 行交叉聚合，模拟大表分析）
  3 × medium （50 万行 GROUP BY，模拟常规取数）
  8 × small  （information_schema 元数据查询，模拟 list_fields 类）

用法（backend 目录）：
  ./.venv/Scripts/python.exe scripts/_load_probe_duckdb.py            # 新实现（游标并发）
  ./.venv/Scripts/python.exe scripts/_load_probe_duckdb.py --legacy   # 模拟旧全局锁实现
"""
import argparse
import statistics as st
import threading
import time

from app.core.duckdb_client import DuckDBClient, duckdb_client

HEAVY = "SELECT count(*) FROM (SELECT 1 FROM range(4500) a CROSS JOIN range(4500) b) x"
MEDIUM = "SELECT i % 100 AS g, count(*) FROM range(500000) AS t(i) GROUP BY 1"
SMALL = "SELECT column_name, data_type FROM information_schema.columns LIMIT 50"


def apply_legacy_patch() -> None:
    """把 fetchall/execute 换回旧实现（主连接 + 全局 _conn_lock，读也串行）。"""

    def legacy_execute(self, query, params=None):
        conn = self._get_connection()
        with self._conn_lock:
            return conn.execute(query, params) if params else conn.execute(query)

    def legacy_fetchall(self, query, params=None):
        conn = self._get_connection()
        with self._conn_lock:
            result = conn.execute(query, params) if params else conn.execute(query)
            return result.fetchall()

    DuckDBClient.execute = legacy_execute
    DuckDBClient.fetchall = legacy_fetchall


def run_one_analysis() -> float:
    """跑一次完整「分析」（12 条查询），返回耗时秒。"""
    t0 = time.perf_counter()
    duckdb_client.fetchall(HEAVY)
    for _ in range(3):
        duckdb_client.fetchall(MEDIUM)
    for _ in range(8):
        duckdb_client.fetchall(SMALL)
    return time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=6, help="并发虚拟用户数（同时分析的人数）")
    ap.add_argument("--rounds", type=int, default=3, help="每人分析轮数")
    ap.add_argument("--legacy", action="store_true", help="模拟旧全局锁实现做对照")
    args = ap.parse_args()
    if args.legacy:
        apply_legacy_patch()

    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def user_worker(uid: int) -> None:
        for _ in range(args.rounds):
            try:
                dt = run_one_analysis()
                with lock:
                    latencies.append(dt)
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(e).__name__}: {e}")

    total_analyses = args.users * args.rounds
    total_queries = total_analyses * 12
    t0 = time.perf_counter()
    threads = [threading.Thread(target=user_worker, args=(i,)) for i in range(args.users)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.perf_counter() - t0

    mode = "LEGACY(全局锁)" if args.legacy else "NEW(游标并发)"
    lat_sorted = sorted(latencies)
    p50 = st.median(lat_sorted) if lat_sorted else float("nan")
    p95 = lat_sorted[int(len(lat_sorted) * 0.95) - 1] if lat_sorted else float("nan")
    print(f"=== 模式: {mode} | 用户={args.users} 轮={args.rounds} ===")
    print(f"完成分析 : {len(latencies)}/{total_analyses}  错误: {len(errors)}")
    if errors:
        print(f"错误样例 : {errors[:2]}")
    print(f"总耗时   : {wall:.2f}s")
    print(f"分析吞吐 : {len(latencies) / wall:.2f} 次/秒（每次 12 条查询）")
    print(f"查询吞吐 : {total_queries / wall:.1f} 条/秒")
    if lat_sorted:
        print(f"单次分析延迟: P50={p50 * 1000:.0f}ms  P95={p95 * 1000:.0f}ms  "
              f"min={lat_sorted[0] * 1000:.0f}ms  max={lat_sorted[-1] * 1000:.0f}ms")


if __name__ == "__main__":
    main()
