# -*- coding: utf-8 -*-
"""会话列表的 running / interrupted 字段（arq 模式下的「刷新自动续收」触发条件）。

【修的 bug】前端唯一的自动续收触发条件 = /sessions 返回的 `running`。
旧实现只查「进程内注册表」：arq 模式下任务跑在**独立 worker 进程**，本进程
chat_registry 里没有它 → running 恒为 False → 刷新后不自动续收。

【修法】running 与 interrupted 合并为「一次读取 ai:chat:task:{sid}、两个字段」：
    in_proc = chat_registry.is_running(sid)      # 进程内模式仍需它保证精度
    status  = json.loads(task_json)["status"]    # 两种模式都会写这个 key
    running = in_proc or status == "running"
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import app.api.v1.ai as ai_module
import app.core.cache as cache_module
from app.models.ai_session import AISession


def _run(coro):
    return asyncio.run(coro)


class _FakeResult:
    def __init__(self, items) -> None:
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeDB:
    """只提供 list_sessions 用到的最小接口（execute → scalars → all）。"""

    def __init__(self, items) -> None:
        self._items = items

    async def execute(self, *args, **kwargs):
        return _FakeResult(self._items)


class _FakeCache:
    def __init__(self, value, *, boom: bool = False) -> None:
        self._value = value
        self._boom = boom
        self.read_keys: list[str] = []

    def get(self, key):
        self.read_keys.append(key)
        if self._boom:
            raise RuntimeError("redis down")
        return self._value


class _FakeRegistry:
    def __init__(self, running: bool = False) -> None:
        self._running = running

    def is_running(self, session_id) -> bool:
        return self._running


def _session(entry: str = "chat") -> AISession:
    return AISession(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model="glm-5.3-flash",
        entry=entry,
        title="会话",
        created_at=datetime.now(timezone.utc),
    )


def _call(sess, *, cache, registry, monkeypatch, entry: str = "chat"):
    """直调 list_sessions（绕开 HTTP 层，注入假 cache / 假注册表）。"""
    monkeypatch.setattr(cache_module, "_cache_repo", cache)
    monkeypatch.setattr(ai_module, "chat_registry", registry)
    return _run(
        ai_module.list_sessions(
            entry=entry,
            canvas_id=None,
            current_user=SimpleNamespace(id=sess.user_id),
            db=_FakeDB([sess]),
        )
    )


def _row(resp) -> dict:
    return resp.data[0]


# ── a) arq 场景：注册表空，但 Redis 任务态 running ──────────────────────────

def test_running_true_from_task_key_when_registry_empty(monkeypatch):
    """**这就是 arq 场景**：本进程注册表没有该任务，但 Redis 里是 running。"""
    sess = _session()
    resp = _call(
        sess,
        cache=_FakeCache(json.dumps({"status": "running"})),
        registry=_FakeRegistry(False),
        monkeypatch=monkeypatch,
    )
    assert _row(resp)["running"] is True
    assert _row(resp)["interrupted"] is False


# ── b) interrupted ──────────────────────────────────────────────────────────

def test_interrupted_true_and_running_false(monkeypatch):
    sess = _session()
    resp = _call(
        sess,
        cache=_FakeCache(json.dumps({"status": "interrupted"})),
        registry=_FakeRegistry(False),
        monkeypatch=monkeypatch,
    )
    assert _row(resp)["running"] is False
    assert _row(resp)["interrupted"] is True


# ── c) 无 key ───────────────────────────────────────────────────────────────

def test_no_task_key_both_false(monkeypatch):
    sess = _session()
    resp = _call(
        sess,
        cache=_FakeCache(None),
        registry=_FakeRegistry(False),
        monkeypatch=monkeypatch,
    )
    assert _row(resp)["running"] is False
    assert _row(resp)["interrupted"] is False


# ── d) cache 抛异常 → 接口不报错、两字段均 False ─────────────────────────────

def test_cache_error_degrades_both_false_without_raising(monkeypatch):
    sess = _session()
    resp = _call(
        sess,
        cache=_FakeCache(None, boom=True),
        registry=_FakeRegistry(False),
        monkeypatch=monkeypatch,
    )
    assert _row(resp)["running"] is False
    assert _row(resp)["interrupted"] is False


# ── 附加：不能为了修 arq 而削弱进程内精度 ───────────────────────────────────

def test_inprocess_registry_still_reported_when_key_missing(monkeypatch):
    """进程内模式：Redis key 缺失时，注册表仍应让 running=True。"""
    sess = _session()
    resp = _call(
        sess,
        cache=_FakeCache(None),
        registry=_FakeRegistry(True),
        monkeypatch=monkeypatch,
    )
    assert _row(resp)["running"] is True


def test_task_key_read_exactly_once(monkeypatch):
    """「一次读取、两个字段」：每个会话只读一次 ai:chat:task:{sid}。"""
    sess = _session()
    cache = _FakeCache(json.dumps({"status": "running"}))
    _call(sess, cache=cache, registry=_FakeRegistry(False), monkeypatch=monkeypatch)
    assert cache.read_keys == [f"ai:chat:task:{sess.id}"]


def test_canvas_entry_does_not_use_inprocess_registry(monkeypatch):
    """画布入口：in_proc 恒 False（本次不扩展画布，保持既有语义），Redis 态照读。"""
    sess = _session(entry="canvas")
    resp = _call(
        sess,
        cache=_FakeCache(json.dumps({"status": "running"})),
        registry=_FakeRegistry(True),
        monkeypatch=monkeypatch,
        entry="canvas",
    )
    # 注册表被忽略，但 Redis 的 running 仍然生效（跨入口真实状态）
    assert _row(resp)["running"] is True
