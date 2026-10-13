# -*- coding: utf-8 -*-
"""事件日志（B）与任务队列（A）的自动化测试。

补上此前只有手工探针覆盖的缺口：
- event_stream 的解析与读写契约（用假客户端，不依赖真实 Redis）
- _stream_from_event_log 的回放语义（跳过非幂等事件、文本合并、兜底 done）
- worker_alive / enqueue_chat_task 的降级路径（无 worker 时回退进程内执行）
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.services.event_stream import _parse_xread, stream_key


def _run(coro):
    return asyncio.run(coro)


# ── 事件流：XREAD 解析（纯函数） ────────────────────────────────────────────

def test_parse_xread_normal():
    # 契约：客户端用 decode_responses=True，因此 key/字段名都是 str
    resp = [["k", [("1-0", {"d": '{"type": "message", "delta": "hi"}'})]]]
    out = _parse_xread(resp)
    assert out == [("1-0", {"type": "message", "delta": "hi"})]


def test_parse_xread_empty_and_malformed():
    assert _parse_xread(None) == []
    assert _parse_xread([]) == []
    # 缺字段 / JSON 坏掉 → 跳过而不是抛异常（日志坏了不能拖垮主链路）
    assert _parse_xread([["k", [("1-0", {})]]]) == []
    assert _parse_xread([["k", [("1-0", {"d": "{not json"})]]]) == []


def test_stream_key_prefix():
    assert stream_key("abc") == "ai:events:abc"


# ── 事件流：publish/read/clear 契约（假客户端，无需 Redis） ──────────────────

class _FakeRedis:
    """最小可用的假 Redis：只实现事件流用到的几个命令。"""

    def __init__(self) -> None:
        self.data: dict[str, list[tuple[str, dict]]] = {}
        self.expires: list[tuple[str, int]] = []
        self._seq = 0

    async def xadd(self, key, fields, maxlen=None, approximate=None):
        self._seq += 1
        eid = f"{self._seq}-0"
        self.data.setdefault(key, []).append((eid, dict(fields)))
        return eid

    async def expire(self, key, ttl):
        self.expires.append((key, ttl))
        return True

    async def xread(self, streams, count=None, block=None):
        out = []
        for key, after in streams.items():
            entries = [e for e in self.data.get(key, []) if e[0] > after]
            if entries:
                out.append([key, entries[: count or 100]])
        return out or None

    async def xlen(self, key):
        return len(self.data.get(key, []))

    async def delete(self, key):
        self.data.pop(key, None)
        return 1


def test_publish_then_read_roundtrip(monkeypatch):
    from app.services import event_stream as mod

    fake = _FakeRedis()
    monkeypatch.setattr(mod.event_stream, "_client", fake, raising=False)
    monkeypatch.setattr(mod.event_stream, "_conn", lambda: fake)

    async def main():
        await mod.event_stream.publish("s1", {"type": "a"})
        await mod.event_stream.publish("s1", {"type": "b"})
        got = await mod.event_stream.read_after("s1", "0")
        assert [ev["type"] for _eid, ev in got] == ["a", "b"]
        # 从第一条之后读 → 只拿到第二条（游标语义）
        got2 = await mod.event_stream.read_after("s1", got[0][0])
        assert [ev["type"] for _eid, ev in got2] == ["b"]
        assert await mod.event_stream.has_events("s1") is True
        await mod.event_stream.clear("s1")
        assert await mod.event_stream.has_events("s1") is False

    _run(main())


def test_publish_sets_ttl(monkeypatch):
    """日志是每会话一个 key，必须设过期（否则无限累积）。"""
    from app.services import event_stream as mod

    fake = _FakeRedis()
    monkeypatch.setattr(mod.event_stream, "_conn", lambda: fake)

    async def main():
        await mod.event_stream.publish("s1", {"type": "a"})
        assert fake.expires == [(stream_key("s1"), mod.STREAM_TTL_SECONDS)]

    _run(main())


# ── _stream_from_event_log 的回放语义 ───────────────────────────────────────

class _FakeEventStream:
    """给 _stream_from_event_log 用的假事件流。"""

    def __init__(self, events, *, tail_events=None, idle_probe=False):
        self._events = list(events)
        self._tail = list(tail_events or [])
        self._idle_probe = idle_probe
        self.cleared: list[str] = []
        self.tail_calls = 0

    async def read_after(self, sid, last_id="0", count=1000):
        return list(self._events)

    async def has_events(self, sid):
        return bool(self._events)

    async def clear(self, sid):
        self.cleared.append(sid)

    async def tail(self, sid, last_id, *, block_ms=0, idle_stop=None):
        self.tail_calls += 1
        for eid, ev in self._tail:
            yield eid, ev
        if self._idle_probe:
            # 模拟"一直阻塞但没有新事件"（worker 中途被杀）：反复问 idle_stop 直到它说停
            for _ in range(2000):
                if idle_stop is not None and idle_stop():
                    return
                await asyncio.sleep(0.01)


def _collect(lines):
    """把 SSE 行解析回事件字典列表。"""
    out = []
    for line in lines:
        assert line.startswith("data: ")
        out.append(json.loads(line[6:].strip()))
    return out


def test_replay_skips_canvas_action_and_coalesces_text(monkeypatch):
    """回放：canvas_action 跳过（非幂等），message 合并为一条。"""
    from app.api.v1 import ai as v1

    fake = _FakeEventStream([
        ("1-0", {"type": "message", "delta": "你"}),
        ("2-0", {"type": "message", "delta": "好"}),
        ("3-0", {"type": "canvas_action", "action": {"action": "add_text_block"}}),
        ("4-0", {"type": "intent", "intent": "chat"}),
        ("5-0", {"type": "done"}),
    ])
    monkeypatch.setattr(v1, "event_stream", fake)

    async def main():
        return [line async for line in v1._stream_from_event_log("s1")]

    events = _collect(_run(main()))
    types = [e["type"] for e in events]
    assert "canvas_action" not in types          # 非幂等副作用不回放
    assert types.count("message") == 1           # 两条 delta 合并成一条
    assert events[0] == {"type": "message", "delta": "你好"}
    assert types[-1] == "done"
    assert fake.tail_calls == 0                  # 日志里已有 done → 不再阻塞续读


def test_replay_tails_until_done(monkeypatch):
    """回放后没有 done → 从游标继续阻塞读，直到读到 done。"""
    from app.api.v1 import ai as v1

    fake = _FakeEventStream(
        [("1-0", {"type": "message", "delta": "A"})],
        tail_events=[("2-0", {"type": "message", "delta": "B"}), ("3-0", {"type": "done"})],
    )
    monkeypatch.setattr(v1, "event_stream", fake)

    async def main():
        return [line async for line in v1._stream_from_event_log("s1")]

    events = _collect(_run(main()))
    types = [e["type"] for e in events]
    assert types == ["message", "message", "done"]
    assert fake.tail_calls == 1


def test_replay_emits_fallback_done_when_log_has_none(monkeypatch):
    """任务硬崩溃（日志无 done）也要补一个 done，否则前端一直停在"思考中"。"""
    from app.api.v1 import ai as v1

    fake = _FakeEventStream([("1-0", {"type": "message", "delta": "半截"})])
    monkeypatch.setattr(v1, "event_stream", fake)

    async def main():
        return [line async for line in v1._stream_from_event_log("s1")]

    events = _collect(_run(main()))
    assert [e["type"] for e in events] == ["message", "done"]


def test_idle_bound_stops_when_no_progress(monkeypatch):
    """worker 中途被杀：日志不再有新事件 → 由 max_idle_seconds 兜底退出并补 done。

    没有这个上限，读者会一直阻塞到 max_seconds（默认 30 分钟）——用户干等。
    """
    from app.api.v1 import ai as v1

    fake = _FakeEventStream([("1-0", {"type": "message", "delta": "半截"})], idle_probe=True)
    monkeypatch.setattr(v1, "event_stream", fake)

    async def main():
        return [line async for line in v1._stream_from_event_log("s1", max_idle_seconds=0.05)]

    events = _collect(_run(main()))
    assert [e["type"] for e in events] == ["message", "done"]


# ── 队列：在线判定与降级 ────────────────────────────────────────────────────

class _FakePool:
    def __init__(self, alive: bool):
        self._alive = alive
        self.enqueued: list[tuple] = []

    async def exists(self, key):
        return 1 if self._alive else 0

    async def enqueue_job(self, name, **payload):
        self.enqueued.append((name, payload))
        return type("J", (), {"job_id": "j1"})()


def test_worker_alive_false_without_heartbeat(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "_pool", _FakePool(alive=False))
    assert _run(worker.worker_alive()) is False


def test_worker_alive_true_with_heartbeat(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "_pool", _FakePool(alive=True))
    assert _run(worker.worker_alive()) is True


def test_enqueue_falls_back_when_no_worker(monkeypatch):
    """没有在线 worker 时必须返回 None（调用方据此回退进程内执行），而不是入队后干等。"""
    from app import worker

    pool = _FakePool(alive=False)
    monkeypatch.setattr(worker, "_pool", pool)
    assert _run(worker.enqueue_chat_task(session_id="s1")) is None
    assert pool.enqueued == []          # 根本不该入队


def test_enqueue_succeeds_when_worker_alive(monkeypatch):
    from app import worker

    pool = _FakePool(alive=True)
    monkeypatch.setattr(worker, "_pool", pool)
    assert _run(worker.enqueue_chat_task(session_id="s1", payload={"a": 1})) == "j1"
    assert pool.enqueued[0][0] == "run_chat_task_job"


# ── 队列消费端：payload 重建 LeadContext ────────────────────────────────────

def test_job_rebuilds_context_from_payload(monkeypatch):
    """worker 侧必须能从可序列化 payload 重建上下文（含窗口轮次）。"""
    from app import worker
    from app.services.agents.lead import LeadContext

    captured = {}

    async def fake_run_chat_task(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("app.services.chat_task.run_chat_task", fake_run_chat_task)

    _run(worker.run_chat_task_job(
        {},
        session_id=None,
        lock_token=None,
        assistant_msg_id=None,
        task_key=None,
        payload={
            "user_id": "u1", "session_id": "", "entry": "chat",
            "history_summary": "口径：含税",
            "memory_covered": 3,
            "metrics_ctx": "- key=a",
            "turns": [{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}],
            "agent_message": "你好",
        },
    ))
    assert captured["session_id"] is None
    factory = captured["event_factory"]
    assert callable(factory)
