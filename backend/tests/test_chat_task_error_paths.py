# -*- coding: utf-8 -*-
"""对话任务的三类异常兜底（P1/P2）回归。

【P1】任务级异常（AI 未配置 / 上游错 / 其他）原先只发 error、**不发 done**：
  前端只在 done 上解锁输入框（error 不解锁），队列模式下续收方也靠日志里的
  done 判断"这轮结束了" → 缺 done 会一直阻塞到 max_idle 兜底（最长数分钟）。

【P2】收尾写终态的条件原为 `not repo.exists(key)`——任务异常时 key 还在
  （本轮写的 running）→ 条件为假 → **不写终态** → key 停在 running →
  前端每次刷新都误判"还在跑"并自动续收（空续收）。
  改为按「本轮标识」归属判定：是自己的 key 才处置。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services import chat_task as ct
from app.services.chat_task import _owns_task_state, run_chat_task
from app.services.llm_client import AIUpstreamError


def _run(coro):
    return asyncio.run(coro)


# ── P2 纯函数：归属判定 ─────────────────────────────────────────────────────

def test_owns_task_state_cases():
    assert _owns_task_state(None, "a") is True, "无 key → 可处置"
    assert _owns_task_state("", "a") is True
    assert _owns_task_state('{"status":"running","assistant_msg_id":"a"}', "a") is True
    assert _owns_task_state('{"status":"running","assistant_msg_id":"b"}', "a") is False, (
        "key 属于新一轮 → 不得处置（否则会把新轮的 running 改成 interrupted）"
    )
    assert _owns_task_state('{"status":"running"}', "a") is True, "无标识（历史数据）→ 可处置"
    assert _owns_task_state("not-json", "a") is True, "坏 JSON → 可处置，不抛异常"


# ── P1/P2 集成：驱动真实 run_chat_task 的异常路径 ────────────────────────────

class _FakeStream:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, session_id, event) -> None:
        self.events.append(event)


class _FakeRegistry:
    def publish(self, session_id, event) -> None:  # noqa: D102
        return None

    def update_text(self, session_id, text) -> None:  # noqa: D102
        return None


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set_shared(self, key, value, ttl=None):
        self.store[key] = value

    def delete_shared(self, key):
        self.store.pop(key, None)

    def set(self, key, value, ttl=None):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class _FakeDB:
    async def get(self, *args, **kwargs):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def patched(monkeypatch):
    stream = _FakeStream()
    cache = _FakeCache()
    monkeypatch.setattr(ct, "event_stream", stream)
    monkeypatch.setattr(ct, "chat_registry", _FakeRegistry())
    monkeypatch.setattr(ct, "get_cache_repository", lambda: cache)
    monkeypatch.setattr(ct, "async_session_factory", lambda: _FakeDB())
    monkeypatch.setattr(ct, "release_session_lock", lambda *a, **k: None)
    return stream, cache


def _failing_factory(exc):
    async def factory(_db):
        raise exc
        yield  # pragma: no cover  （使其成为 async generator）

    return factory


def test_task_level_error_emits_done(patched):
    """P1：任务级异常必须补 done（否则前端锁死到 max_idle 兜底）。"""
    stream, _cache = patched
    task_key = "ai:chat:task:s1"

    _run(run_chat_task(
        session_id="s1", lock_token="t", assistant_msg_id="m1",
        task_key=task_key, event_factory=_failing_factory(AIUpstreamError("boom")),
    ))

    types = [e.get("type") for e in stream.events]
    assert "error" in types, "应有 error 事件"
    assert types[-1] == "done", f"最后一个事件必须是 done，实际={types}"
    assert stream.events[-1].get("degraded") is True


def test_unexpected_error_also_emits_done(patched):
    """任意异常（非上游错）同样补 done。"""
    stream, _cache = patched
    _run(run_chat_task(
        session_id="s1", lock_token="t", assistant_msg_id="m1",
        task_key="ai:chat:task:s1", event_factory=_failing_factory(RuntimeError("x")),
    ))
    assert [e.get("type") for e in stream.events][-1] == "done"


def test_failed_task_writes_interrupted_not_stuck_running(patched):
    """P2：任务异常结束时，任务态必须写成 interrupted，不能停在 running。"""
    _stream, cache = patched
    task_key = "ai:chat:task:s1"
    # 模拟 API 在派发前写的 running（带本轮标识）
    cache.store[task_key] = json.dumps({"status": "running", "assistant_msg_id": "m1"})

    _run(run_chat_task(
        session_id="s1", lock_token="t", assistant_msg_id="m1",
        task_key=task_key, event_factory=_failing_factory(AIUpstreamError("boom")),
    ))

    state = json.loads(cache.store[task_key])
    assert state["status"] == "interrupted", (
        f"异常结束后应写 interrupted，实际={state}（停在 running 会导致前端反复空续收）"
    )


def test_cleanup_does_not_clobber_newer_turn_state(patched):
    """P2：收尾不得覆盖「新一轮」写的状态（归属判定）。"""
    _stream, cache = patched
    task_key = "ai:chat:task:s1"
    # key 已被新一轮接管（标识不是本轮）
    cache.store[task_key] = json.dumps({"status": "running", "assistant_msg_id": "NEW"})

    _run(run_chat_task(
        session_id="s1", lock_token="t", assistant_msg_id="m1",
        task_key=task_key, event_factory=_failing_factory(AIUpstreamError("boom")),
    ))

    state = json.loads(cache.store[task_key])
    assert state["assistant_msg_id"] == "NEW", "不得动新一轮的状态"
    assert state["status"] == "running"


def test_completed_task_deletes_key(patched):
    """正常完成：删除任务态（前端据此读历史，不自动续收）。"""
    _stream, cache = patched
    task_key = "ai:chat:task:s1"
    cache.store[task_key] = json.dumps({"status": "running", "assistant_msg_id": "m1"})

    async def ok_factory(_db):
        yield {"type": "text", "content": "你好"}
        yield {"type": "done"}

    _run(run_chat_task(
        session_id="s1", lock_token="t", assistant_msg_id="m1",
        task_key=task_key, event_factory=ok_factory,
    ))

    assert task_key not in cache.store, "正常完成后应删除任务态 key"

# ── P3：执行者已死时的「无进展」上限 ────────────────────────────────────────

def test_queue_idle_cap_shorter_when_no_worker(monkeypatch):
    """没有在线 worker：任务不可能有进展 → 用更短上限快速收尾（15s）。"""
    import app.api.v1.ai as ai_module
    import app.worker as worker_module

    async def no_worker():
        return False

    monkeypatch.setattr(worker_module, "worker_alive", no_worker)
    assert _run(ai_module._queue_idle_cap()) == 15.0


def test_queue_idle_cap_normal_when_worker_alive(monkeypatch):
    """有在线 worker：120s（与 LLM 超时对齐，避免误判长任务已死）。"""
    import app.api.v1.ai as ai_module
    import app.worker as worker_module

    async def alive():
        return True

    monkeypatch.setattr(worker_module, "worker_alive", alive)
    assert _run(ai_module._queue_idle_cap()) == 120.0


def test_queue_idle_cap_falls_back_when_check_raises(monkeypatch):
    """在线判定本身异常 → 回退 120s（宁可多等，不要误杀正在跑的任务）。"""
    import app.api.v1.ai as ai_module
    import app.worker as worker_module

    async def boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(worker_module, "worker_alive", boom)
    assert _run(ai_module._queue_idle_cap()) == 120.0
