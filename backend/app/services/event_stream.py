"""会话事件流（Redis Stream）：把"进程内广播"升级为"可回放的事件日志"。

原理（为什么需要它）
--------------------
SSE 是一条**只能往前读**的流：读者（浏览器）中途断线后无法"接上"——除非事件本身被
持久化成**带编号的日志**。Redis Stream 正是这种结构：

    XADD  key * d '{json}'        # 追加一条，返回全局递增 ID（毫秒时间戳-序号）
    XREAD COUNT n STREAMS key id  # 读 ID 之后的事件（id=0 表示从头）
    XREAD BLOCK ms STREAMS key id # 阻塞等待新事件（超时返回空）

于是"断线续传"退化成一个纯数据问题：**前端带上最后收到的 ID，服务端从它之后读**——
不重复、不丢失。

与其他 Redis 结构的取舍
----------------------
- List(LPUSH/BRPOP)：读完即删，多读者互相抢，不能广播  → 不适合
- Pub/Sub：纯广播但**不存储**，订阅者不在线就永久丢失   → 正是旧实现丢块的原因
- Stream：既存储又广播，可按各自游标读                  → 采用

与旧实现的差别
--------------
旧：只有"文本快照"（一个字符串）可续收，canvas_action / decision / progress 刷新即丢。
新：所有事件都在日志里按 ID 续读；文本只是日志中 message 事件的累加。

降级策略
--------
Redis 不可用或写失败时静默返回 None——主链路（进程内注册表广播）不受影响，
续收退化为旧的文本快照路径。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from app.config import settings

logger = logging.getLogger("lvco.services.event_stream")

STREAM_PREFIX = "ai:events:"
# 单会话事件日志上限：一轮对话的事件量级是几百条，2000 足够且防无限增长
STREAM_MAXLEN = 2000
# 阻塞读的单次窗口（毫秒）：醒来的目的是检查"任务是否已结束"，不必等太久
STREAM_BLOCK_MS = 10000
# socket 读超时必须**显著大于** BLOCK 窗口，否则 XREAD 的阻塞会被读超时打断。
# 实测（redis-py 8.1.0）：不显式设置时存在约 5s 的默认读超时——block=4000 正常、
# block=5000 必在 5.0s 抛 TimeoutError，且默认 3 次重试会把它拖成 ~15s 后才失败
# （症状：续收连接在阻塞读处静默断开，前端表现为"流突然结束、收不到 done"）。
# 这里显式给 30s，让 BLOCK 的语义真正生效。
STREAM_SOCKET_TIMEOUT = 30.0


def stream_key(session_id: object) -> str:
    return f"{STREAM_PREFIX}{session_id}"


class EventStream:
    """事件日志读写（懒连接；任何异常都降级为"无日志"，不打断主链路）。"""

    def __init__(self) -> None:
        self._client: Any = None

    # ── 连接 ────────────────────────────────────────────────────────────────
    def _conn(self) -> Any:
        if self._client is None:
            try:
                import redis.asyncio as aioredis

                self._client = aioredis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_timeout=STREAM_SOCKET_TIMEOUT,   # 必须 > BLOCK 窗口，见文件头说明
                )
            except Exception:  # noqa: BLE001
                logger.warning("event_stream_connect_failed", exc_info=True)
                return None
        return self._client

    # ── 写：追加事件 ────────────────────────────────────────────────────────
    async def publish(self, session_id: object, event: dict) -> str | None:
        """XADD 追加一条事件，返回事件 ID（失败返回 None）。"""
        client = self._conn()
        if client is None or session_id is None:
            return None
        try:
            return await client.xadd(
                stream_key(session_id),
                {"d": json.dumps(event, ensure_ascii=False, default=str)},
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        except Exception:  # noqa: BLE001
            logger.debug("event_stream_publish_failed", exc_info=True)
            return None

    # ── 读：按游标回放 ──────────────────────────────────────────────────────
    async def read_after(
        self, session_id: object, last_id: str = "0", count: int = 1000
    ) -> list[tuple[str, dict]]:
        """读 ID 严格大于 last_id 的事件（last_id="0" 表示从头）。"""
        client = self._conn()
        if client is None or session_id is None:
            return []
        try:
            resp = await client.xread(
                {stream_key(session_id): last_id}, count=count
            )
        except Exception:  # noqa: BLE001
            logger.debug("event_stream_read_failed", exc_info=True)
            return []
        return _parse_xread(resp)

    async def tail(
        self,
        session_id: object,
        last_id: str,
        *,
        block_ms: int = STREAM_BLOCK_MS,
        idle_stop: Any = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """阻塞续读新事件；连续空转且 idle_stop() 为真时退出。

        idle_stop 用来回答"任务是否还在跑"——阻塞窗口到期没有新事件且任务已结束，
        说明日志读完了，可以收尾。
        """
        client = self._conn()
        if client is None or session_id is None:
            return
        cursor = last_id
        while True:
            try:
                resp = await client.xread(
                    {stream_key(session_id): cursor}, count=200, block=block_ms
                )
            except (asyncio.TimeoutError, TimeoutError):
                # 阻塞窗口到期（或读超时早于窗口）——语义等价于"本窗口没有新事件"，
                # 继续下一轮，而不是结束续收。防御 socket_timeout 与 block 的任何错配。
                resp = None
            except Exception:  # noqa: BLE001
                logger.warning("event_stream_tail_failed", exc_info=True)
                return
            items = _parse_xread(resp)
            if items:
                for eid, ev in items:
                    cursor = eid
                    yield eid, ev
                continue
            # 阻塞窗口内没有新事件
            if idle_stop is not None and idle_stop():
                return

    # ── 维护 ────────────────────────────────────────────────────────────────
    async def clear(self, session_id: object) -> None:
        """删除整个事件日志（新一轮开始时调用，避免跨轮回放）。"""
        client = self._conn()
        if client is None or session_id is None:
            return
        try:
            await client.delete(stream_key(session_id))
        except Exception:  # noqa: BLE001
            logger.debug("event_stream_clear_failed", exc_info=True)


def _parse_xread(resp: Any) -> list[tuple[str, dict]]:
    """把 XREAD 返回值解析成 [(event_id, event_dict)]。"""
    out: list[tuple[str, dict]] = []
    if not resp:
        return out
    for _key, entries in resp:
        for eid, fields in entries:
            raw = (fields or {}).get("d")
            if not raw:
                continue
            try:
                out.append((str(eid), json.loads(raw)))
            except (json.JSONDecodeError, TypeError):
                continue
    return out


# 进程级单例（与 chat_registry 同风格）
event_stream = EventStream()
