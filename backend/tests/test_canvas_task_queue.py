# -*- coding: utf-8 -*-
"""画布入口搬上 ARQ worker（主入口的后台化收口）。

此前只有对话入口走队列（TASK_QUEUE_ENABLED 只被 chat/stream 消费），画布——
**主入口**——的任务体整体闭在 API 请求作用域里：API 重启/发布，正在跑的画布任务
直接消失。本次补齐三件事，测试逐条钉死：

  1. worker 注册 run_canvas_task_job（没有它，入队只会永远排队）；
  2. 画布端点有入队分支 + 失败回退进程内（与对话入口同构）；
  3. 工具白名单两处定义一致（worker 不能 import API 层，复制名单必须防漂移）；
  4. run_canvas_task 内核的关键翻译行为：canvas_action 提取、done 抑制到流尾、
     空文本兜底文案、收尾清任务态/释放锁。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import canvas_task as CT

BACKEND = Path(__file__).resolve().parents[1]


# ── 注册与契约 ─────────────────────────────────────────────────────────────

def test_worker_registers_canvas_job():
    from app.worker import WorkerSettings

    names = {f.__name__ for f in WorkerSettings.functions}
    assert "run_chat_task_job" in names
    assert "run_canvas_task_job" in names, "画布任务未注册进 worker（入队会永远排队）"


def test_canvas_endpoint_has_queue_branch_with_fallback():
    """端点源码必须包含：入队调用 + 失败回退标记 + 队列模式从事件日志读取。"""
    src = (BACKEND / "app" / "api" / "v1" / "ai.py").read_text(encoding="utf-8")
    assert "enqueue_canvas_task(" in src
    assert "canvas_task_enqueue_failed_fallback_inprocess" in src
    # 队列模式：先入队判断，再 registry.start —— queued 时必须走事件日志
    assert "queued or chat_registry.start" in src
    assert "_stream_from_event_log(" in src


def test_worker_canvas_tool_whitelist_matches_api():
    """worker 不 import API 层 → 名单复制了一份，必须防漂移。"""
    import re

    from app.worker import _CANVAS_QUERY_TOOL_NAMES

    ai_src = (BACKEND / "app" / "api" / "v1" / "ai.py").read_text(encoding="utf-8")
    m = re.search(r"_CANVAS_QUERY_TOOL_NAMES = frozenset\(\{(.*?)\}\)", ai_src, re.DOTALL)
    assert m, "ai.py 名单结构变化，请同步更新本测试"
    api_names = {s.strip().strip('"\'') for s in m.group(1).split(",") if s.strip()}
    assert api_names == set(_CANVAS_QUERY_TOOL_NAMES)


# ── run_canvas_task 内核行为 ───────────────────────────────────────────────

class _FakeRepo:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def exists(self, key):
        return key in self.store

    def get(self, key):
        return self.store.get(key)

    def set_shared(self, key, value, ttl=None):
        self.store[key] = value

    def delete_shared(self, key):
        self.store.pop(key, None)


@pytest.fixture()
def fake_cache(monkeypatch):
    repo = _FakeRepo()
    monkeypatch.setattr(CT, "get_cache_repository", lambda: repo)
    # 事件日志用假实现：记录 publish，tail 立即结束
    published: list[dict] = []

    class _FakeStream:
        async def publish(self, sid, ev):
            published.append(ev)

        async def has_events(self, sid):
            return bool(published)

    monkeypatch.setattr(CT, "event_stream", _FakeStream())
    monkeypatch.setattr(CT, "release_session_lock", lambda sid, tok: None)
    return repo, published


class _FakeAIMessage:
    def __init__(self) -> None:
        self.content = ""


async def _run_canvas_task(events, monkeypatch, fake_cache, *, assistant_id="m1"):
    """驱动 run_canvas_task：fake event_factory 吐给定事件；db.get 返回假占位行。"""
    repo, published = fake_cache

    class _FakeDB:
        async def get(self, model, mid):
            return _FakeAIMessage()

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def close(self):
            return None

    async def event_factory(db_session):
        for ev in events:
            yield ev

    # 替换 async_session_factory 为返回 FakeDB 的异步上下文管理器
    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(CT, "async_session_factory", lambda: _FakeSessionCtx())
    await CT.run_canvas_task(
        session_id="s-1", lock_token="t", assistant_msg_id=assistant_id,
        task_key="ai:canvas:task:s-1", event_factory=event_factory,
    )
    return published, repo


async def test_canvas_action_extracted_and_done_suppressed(monkeypatch, fake_cache):
    """tool_result 内嵌 canvas_action → 独立事件；Agent 内部 done 被抑制，流尾只发一次。"""
    tool_result = json.dumps({
        "ok": True,
        "canvas_action": {"action": "add_chart_block", "blockId": "b1"},
    })
    published, _ = await _run_canvas_task([
        {"type": "tool_result", "name": "add_chart_block", "result": tool_result},
        {"type": "done", "degraded": False},   # Agent 内部 done → 必须被抑制
    ], monkeypatch, fake_cache)

    types = [e.get("type") for e in published]
    assert types.count("canvas_action") == 1
    assert types.count("done") == 1, "Agent 内部 done 必须抑制，流尾统一下发"


async def test_empty_text_with_actions_gets_fallback_content(monkeypatch, fake_cache):
    """全程只调工具（无文本）→ 落库内容用兜底文案，保证会话链路完整。"""
    tool_result = json.dumps({
        "ok": True, "canvas_action": {"action": "add_chart_block", "blockId": "b1"},
    })
    published, _ = await _run_canvas_task([
        {"type": "tool_result", "name": "add_chart_block", "result": tool_result},
    ], monkeypatch, fake_cache)
    assert any("画布工具完成" in str(e) for e in published) is False  # 兜底文案进落库而非事件流
    assert published[-1]["type"] == "done"


async def test_interrupted_task_marks_state(monkeypatch, fake_cache):
    """任务体异常 → error + done(degraded) + 任务态写 interrupted（前端不再误报在跑）。"""
    repo, published = fake_cache

    class _FakeDB:
        async def get(self, model, mid):
            return _FakeAIMessage()

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def close(self):
            return None

    class _FakeSessionCtx:
        async def __aenter__(self):
            return _FakeDB()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(CT, "async_session_factory", lambda: _FakeSessionCtx())

    # "running" 状态由 API 侧启动时写入（本轮自己的）→ 收尾允许写 interrupted 终态
    repo.store["ai:canvas:task:s-1"] = json.dumps({"status": "running", "assistant_msg_id": "m1"})

    async def _factory(db):
        yield {"type": "text", "content": "部分内容"}
        raise RuntimeError("boom")

    await CT.run_canvas_task(
        session_id="s-1", lock_token="t", assistant_msg_id="m1",
        task_key="ai:canvas:task:s-1", event_factory=_factory,
    )
    types = [e.get("type") for e in published]
    assert "error" in types and types.count("done") == 1
    task_state = json.loads(repo.store.get("ai:canvas:task:s-1", "{}"))
    assert task_state.get("status") == "interrupted"