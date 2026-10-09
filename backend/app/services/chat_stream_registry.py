"""对话入口后台续跑注册表：把「Agent 执行任务」与「SSE 订阅连接」解耦。

为什么需要它：
- 旧实现：Agent 事件流挂在请求的 SSE generator 上，客户端一断开（刷新/切页），
  asyncio 取消整个 generator → Agent 被杀 → 回复只剩一条空的"未完成"占位行。
- 新实现：Agent 以独立 asyncio.Task 在进程内跑，连接只负责订阅事件队列；
  连接断开只退订、不杀任务；刷新后带同一 session_id 重连时命中运行中的任务，
  直接续收已产出的事件，任务结束由任务协程自行落库。

进程内单例即可（uvicorn 默认单 worker）。将来多 worker 部署时需要替换为
基于 Redis pub/sub + 任务状态的实现（本类接口形态保持不变，替换成本可控）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncIterator, Awaitable, Callable

logger = logging.getLogger("lvco.services.chat_stream_registry")

# 任务向订阅者广播的结束哨兵（订阅者收到后退出订阅循环，正常业务事件不会用这个 type）
_END_EVENT = {"type": "__end__"}

# 异步迭代器工厂：零参可调用，返回一个 async-iterable（任务主体）
CoroFactory = Callable[[], Awaitable]


class RunningTask:
    """一个正在后台执行的会话任务：执行 Task + 一组订阅队列 + 可恢复文本快照。"""

    __slots__ = ("session_id", "task", "queues", "full_text", "last_redis_write")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.task: asyncio.Task | None = None
        self.queues: set[asyncio.Queue] = set()
        # 已产出的可见文本全量累加：重连续收时先补发一次，再续收增量（避免只看到后半段）
        self.full_text = ""
        self.last_redis_write = 0.0  # Redis 全量快照节流时间戳


class ChatStreamRegistry:
    """进程内单例。线程安全：start/remove 与 get 由锁保护，publish/subscribe 无锁。"""

    def __init__(self) -> None:
        self._tasks: dict[str, RunningTask] = {}
        self._lock = threading.Lock()

    # ── 查询 ────────────────────────────────────────────────────
    def get(self, session_id: str) -> RunningTask | None:
        with self._lock:
            return self._tasks.get(session_id)

    def is_running(self, session_id: str) -> bool:
        return self.get(session_id) is not None

    # ── 任务生命周期 ────────────────────────────────────────────
    def start(self, session_id: str, coro_factory: CoroFactory) -> bool:
        """注册并调度一个新的后台任务。

        coro_factory 是零参可调用，返回任务主体协程。任务协程负责消费 Agent
        事件流、把每个事件发布给订阅者（registry.publish / update_text），并在
        结束时自行落库、释放会话锁（见 ai.py 的 _run_chat_task）——注册表不为它
        做任何业务收尾，只负责异常兜底与注册表摘除。

        Returns:
            True = 本次调用创建了任务；False = 同会话已有任务在跑（调用方应转订阅模式）。
            返回 False 的情况也可能发生在 start 之后瞬间，因此调用方随后应统一走
            subscribe 收尾，不要直接抛错。
        """
        with self._lock:
            if session_id in self._tasks:
                return False
            rt = RunningTask(session_id)
            self._tasks[session_id] = rt

        async def _runner() -> None:
            # 任务协程自身已将副作用（落库/释放锁/事件广播）收拾干净；
            # 这里只负责：异常兜底广播 + 结束哨兵 + 从注册表摘除。
            try:
                await coro_factory()
            except Exception:
                logger.exception("chat_task_crashed session=%s", session_id)
                self._publish(rt, {"type": "error", "message": "后台任务异常终止"})
            finally:
                self._publish(rt, _END_EVENT)
                with self._lock:
                    if self._tasks.get(session_id) is rt:
                        self._tasks.pop(session_id, None)

        rt.task = asyncio.create_task(_runner(), name=f"chat-task-{session_id}")
        return True

    def remove(self, session_id: str) -> None:
        """（一般无需手动调用）任务结束后由 _runner 自动摘除，这里保留供测试/清理用。"""
        with self._lock:
            self._tasks.pop(session_id, None)

    # ── 事件广播 / 订阅 ─────────────────────────────────────────
    def publish(self, session_id: str, ev: dict) -> None:
        rt = self.get(session_id)
        if rt is not None:
            self._publish(rt, ev)

    def update_text(self, session_id: str, full_text: str) -> None:
        """任务侧落地最新可见文本快照（重连续收时补发用）。"""
        rt = self.get(session_id)
        if rt is not None:
            rt.full_text = full_text

    def _publish(self, rt: RunningTask, ev: dict) -> None:
        if not rt.queues:
            return
        # 复制一份再逐个投递：iterator 中订阅者可能退订并清空 set
        for q in list(rt.queues):
            try:
                q.put_nowait(ev)
            except Exception:
                rt.queues.discard(q)

    async def subscribe(self, session_id: str) -> AsyncIterator[dict]:
        """订阅指定会话的运行中任务：事件进队→出队 yield，任务结束（收到哨兵）自然退出。

        无任务时为空迭代器（调用方自行决定是补发 Redis 快照还是直接收尾）。
        """
        rt = self.get(session_id)
        if rt is None:
            return
        q: asyncio.Queue = asyncio.Queue()
        rt.queues.add(q)
        try:
            while True:
                ev = await q.get()
                if ev.get("type") == "__end__":
                    break
                yield ev
        finally:
            rt.queues.discard(q)