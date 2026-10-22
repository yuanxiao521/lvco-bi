# -*- coding: utf-8 -*-
"""跨进程 DuckDB 写窗口协调（方案 B）：读者下线 → 写方独占 → 读者回来。

背景（实测数据）：DuckDB 文件锁 = 多读并行 / 一写独占（读写互斥一切）。
API 与 worker 各持只读连接后，导入/删源的 write_window 会永远打不开文件——
除非所有读者主动下线。本协调器用 Redis 心跳 + 写意图广播完成闭环。

测试用 fake cache repo（不需要真 Redis），锁三件事：
  1. RO 连接打开 → 注册读者心跳；写窗口进入 → 心跳下线、意图广播；
  2. 写方等待**其它**读者下线（自己的不算），超时给明确报错而不是挂死；
  3. 意图撤销：finally 必清（否则读者永远不回来）。
"""
from __future__ import annotations

import threading

import pytest
import duckdb

from app.core import duckdb_client as DC


class _FakeRepo:
    """最小 Redis 替身：get/set_shared/delete_shared/scan_shared_keys。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set_shared(self, key, value, ttl=None):
        self.store[key] = value

    def delete_shared(self, key):
        self.store.pop(key, None)

    def scan_shared_keys(self, prefix):
        return [k for k in self.store if k.startswith(prefix)]


@pytest.fixture()
def coord(monkeypatch):
    repo = _FakeRepo()
    monkeypatch.setattr(DC, "_coordination", DC._DuckWriteCoordination())
    coord = DC._coordination
    monkeypatch.setattr(coord, "_repo", lambda: repo)
    coord._pid = "111"
    return coord, repo


def test_foreign_readers_excludes_self(coord):
    coord, repo = coord
    repo.store["duckdb:reader:111"] = "1"     # 自己
    repo.store["duckdb:reader:222"] = "1"     # 别人
    assert coord.foreign_readers() == ["duckdb:reader:222"]


def test_intent_roundtrip(coord):
    coord, repo = coord
    coord.set_intent("tok")
    assert repo.store.get("duckdb:write-intent") == "tok"
    coord.clear_intent()
    assert "duckdb:write-intent" not in repo.store


def test_write_window_waits_for_foreign_readers(coord, monkeypatch):
    """有其它读者时写窗口必须等（轮询），读者下线后立即放行——而不是直接失败。"""
    coord, repo = coord
    calls = {"n": 0}

    def fake_readers():
        calls["n"] += 1
        return ["duckdb:reader:222"] if calls["n"] <= 2 else []

    monkeypatch.setattr(coord, "foreign_readers", fake_readers)
    # 不真连 DuckDB：把 _connect 替换为返回哨兵的 fake
    sentinel = object()
    opened = {}

    def fake_connect(path, read_only=False):
        opened["read_only"] = read_only
        return sentinel

    client = DC.duckdb_client
    monkeypatch.setattr(client, "_connect", fake_connect)
    monkeypatch.setattr(client, "_db_path", lambda: DC.Path("x.duckdb"))

    # 休眠去掉（fake_readers 第二次就放行），避免测试拖慢
    monkeypatch.setattr(DC, "_INTENT_WAIT_TIMEOUT", 1)

    with client.write_window() as got:
        assert got is sentinel
        assert opened["read_only"] is False          # 写窗口内是读写连接
        assert repo.store.get("duckdb:write-intent") # 窗口内意图在
    # finally：连接清空 + 意图撤销
    assert client._conn is None
    assert "duckdb:write-intent" not in repo.store


def test_write_window_timeout_reports_clearly(coord, monkeypatch):
    """其它读者一直不下线 → 超时抛出**中文明确报错**（不是挂死、不是静默失败）。"""
    coord, repo = coord
    monkeypatch.setattr(coord, "foreign_readers", lambda: ["duckdb:reader:999"])
    monkeypatch.setattr(DC, "_INTENT_WAIT_TIMEOUT", 0.2)
    monkeypatch.setattr(DC.time, "sleep", lambda s: None)

    client = DC.duckdb_client
    monkeypatch.setattr(client, "_db_path", lambda: DC.Path("x.duckdb"))
    with pytest.raises(duckdb.Error) as ei:
        with client.write_window():
            pass
    assert "只读进程" in str(ei.value)


def test_rw_mode_bypasses_coordination(coord, monkeypatch):
    """DUCKDB_READ_ONLY=false（单进程部署）：写窗口直接透传，不碰协调。"""
    coord, repo = coord
    monkeypatch.setattr(DC.settings, "DUCKDB_READ_ONLY", False)
    sentinel = object()
    client = DC.duckdb_client
    monkeypatch.setattr(client, "_get_connection", lambda: sentinel)

    with client.write_window() as got:
        assert got is sentinel
    assert "duckdb:write-intent" not in repo.store   # 没广播过意图
