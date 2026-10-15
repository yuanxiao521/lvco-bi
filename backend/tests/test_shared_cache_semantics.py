# -*- coding: utf-8 -*-
"""跨进程共享键的缓存语义（arq 模式「刷新自动续收」bug 的根因回归）。

【bug】任务态 ai:chat:task:{sid} 走通用 set（**双写**：Redis + 本进程内存副本）。
  API 进程写 running → worker 进程 delete（只删得到 Redis，worker 内存里本没有）
  → API 进程 get：Redis miss → **回退命中自己内存副本的旧值**
  → 任务完成后接口仍报 running=True（幽灵数据），前端自动续收误触发。

【修】共享键专用方法：set_shared / delete_shared **只操作 Redis**，
  内存副本是进程私有的，不该承载跨进程共享状态。
"""
from __future__ import annotations

import json

import pytest

from app.repositories.fallback_cache import FallbackCacheRepository

K = "test:shared:ghost"


@pytest.fixture()
def two_repos():
    """两个独立实例，模拟 API 进程与 worker 进程（各自的内存副本互不相通）。"""
    api_repo = FallbackCacheRepository()
    worker_repo = FallbackCacheRepository()
    if not (api_repo._use_redis and worker_repo._use_redis):
        pytest.skip("Redis 不可用：共享键语义需要真实 Redis 才能验证跨进程行为")
    api_repo.delete_shared(K)
    api_repo.delete(K)
    yield api_repo, worker_repo
    api_repo.delete_shared(K)
    api_repo.delete(K)


def test_shared_delete_kills_cross_process_ghost(two_repos):
    """**回归主用例**：共享键跨进程删除后，原写入进程不得再读到幽灵值。"""
    api_repo, worker_repo = two_repos

    api_repo.set_shared(K, json.dumps({"status": "running"}))
    assert api_repo.get(K) is not None, "写入后应可见"

    worker_repo.delete_shared(K)      # worker 进程删（只删得到 Redis）

    assert api_repo.get(K) is None, (
        "跨进程删除后，原写入进程仍读到幽灵值——共享键泄漏到了内存副本"
    )


def test_shared_set_does_not_pollute_process_memory(two_repos):
    """set_shared 只写 Redis：本进程内存副本必须为空（否则就是回到了双写）。"""
    api_repo, worker_repo = two_repos

    api_repo.set_shared(K, "v1")

    assert api_repo._memory.get(K) is None, "set_shared 不应写进程内存副本"
    assert worker_repo.get(K) == "v1", "其他进程（Redis 优先）应能读到"


def test_shared_roundtrip_and_overwrite(two_repos):
    api_repo, worker_repo = two_repos

    api_repo.set_shared(K, json.dumps({"status": "running"}))
    assert json.loads(worker_repo.get(K))["status"] == "running"

    api_repo.set_shared(K, json.dumps({"status": "interrupted"}))
    assert json.loads(worker_repo.get(K))["status"] == "interrupted"


def test_shared_writes_are_noop_when_redis_unavailable():
    """Redis 不可用时共享键**不写任何副本**（含内存）——这是刻意的取舍，不是 bug。

    为什么不用内存兜底：内存副本是进程私有的，写它会造成"本进程看得到、
    别的进程看不到"的假象，并在 Redis 恢复后重新引入幽灵态。
    代价：Redis 全程不可用时 running/interrupted 标记缺失（前端不自动续收，
    手动 resume 仍可用）——宁可状态缺失，也不给一份可能错的答案。
    注：FallbackCacheRepository 是进程级单例，_use_redis 在首次构造时确定。
    """
    repo = FallbackCacheRepository()
    repo._use_redis = False  # 强制模拟 Redis 不可用（新实例，不影响单例）
    ks, kg = K + ":shared", K + ":generic"

    repo.set_shared(ks, "v")
    assert repo._memory.get(ks) is None, "Redis 不可用时 set_shared 不应写内存副本"
    assert repo.get(ks) is None, "共享键在 Redis 不可用时读不到（刻意：宁可缺失）"

    repo.set(kg, "v")
    assert repo._memory.get(kg) == "v", "通用 set 的降级语义必须保持不变"
    assert repo.get(kg) == "v"

    repo.delete_shared(ks)   # 不应抛异常，也不应误删别的键
    assert repo.get(kg) == "v"


def test_legacy_set_delete_semantics_unchanged(two_repos):
    """通用 set/delete 的双写语义保持不变（session_lock 等单进程场景依赖它）。"""
    api_repo, _ = two_repos

    api_repo.set(K, "v")
    assert api_repo.get(K) == "v"
    api_repo.delete(K)
    assert api_repo.get(K) is None
