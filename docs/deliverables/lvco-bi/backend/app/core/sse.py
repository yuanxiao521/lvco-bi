"""SSE (Server-Sent Events) 工具 - 管理用户级连接池并广播事件

设计:
- 每个 user_id 维护一个 set[asyncio.Queue]，支持多端登录
- publish() 向该 user 所有 queue 写入事件（非阻塞，队列满则丢弃）
- subscribe() 返回一个 asyncio.Queue，FastAPI 端点用 StreamingResponse 消费
- 事件格式遵循 EventSource 协议: `event: <type>\ndata: <json>\n\n`
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from typing import Any

import structlog

log = structlog.get_logger("sse")

# 每个用户队列容量上限（防止慢客户端堆积）
_QUEUE_MAXSIZE = 100


class SSEManager:
    """用户级 SSE 连接池"""

    def __init__(self) -> None:
        # user_id -> set[asyncio.Queue]
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, user_id: uuid.UUID) -> asyncio.Queue:
        """订阅指定用户的事件流，返回一个新 Queue"""
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers[user_id].add(q)
        log.info("sse_subscribed", user_id=str(user_id), total=len(self._subscribers[user_id]))
        return q

    async def unsubscribe(self, user_id: uuid.UUID, q: asyncio.Queue) -> None:
        """取消订阅"""
        async with self._lock:
            if q in self._subscribers.get(user_id, set()):
                self._subscribers[user_id].discard(q)
                if not self._subscribers[user_id]:
                    del self._subscribers[user_id]
        log.info("sse_unsubscribed", user_id=str(user_id))

    async def publish(self, user_id: uuid.UUID, event: str, data: dict[str, Any]) -> int:
        """向指定用户的所有订阅者广播事件，返回成功投递数

        队列满则丢弃（慢客户端不阻塞快客户端）
        """
        delivered = 0
        async with self._lock:
            queues = list(self._subscribers.get(user_id, set()))

        for q in queues:
            try:
                q.put_nowait((event, data))
                delivered += 1
            except asyncio.QueueFull:
                log.warning("sse_queue_full", user_id=str(user_id))
                # 丢弃最旧的一条，放入新的（保证最新事件能送达）
                try:
                    q.get_nowait()
                    q.put_nowait((event, data))
                    delivered += 1
                except Exception:
                    pass
        log.info("sse_published", user_id=str(user_id), event=event, delivered=delivered, total=len(queues))
        return delivered

    def format_event(self, event: str, data: dict[str, Any]) -> str:
        """格式化为 EventSource 协议文本"""
        payload = json.dumps(data, ensure_ascii=False, default=str)
        return f"event: {event}\ndata: {payload}\n\n"

    def subscriber_count(self, user_id: uuid.UUID) -> int:
        return len(self._subscribers.get(user_id, set()))


# 单例
sse_manager = SSEManager()
