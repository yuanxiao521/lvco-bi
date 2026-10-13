# -*- coding: utf-8 -*-
"""第 4 课：阻塞读的坑——BLOCK 时长必须小于 socket 超时 ⭐

这是我在真实项目里踩的坑，值得单独一课。

XREAD BLOCK 10000 的意思是"最多等 10 秒，有新事件就立刻返回"。
但它底层是一次 socket 读——如果 socket 自己的读超时更短，
就会在读超时时抛错，而不是安静地等满 10 秒。

跑法：.venv/Scripts/python.exe learning/04_block_and_timeout.py
"""
import asyncio
import time

import redis.asyncio as aioredis

KEY = "learning:block"


async def 测(说明, *, block_ms, socket_timeout):
    r = aioredis.from_url(
        "redis://localhost:6379/0", decode_responses=True, socket_timeout=socket_timeout
    )
    await r.delete(KEY)
    t0 = time.time()
    try:
        # 空 stream + BLOCK：期望"等满 block_ms 后返回空"
        await r.xread({KEY: "0"}, count=10, block=block_ms)
        print(f"  {说明:38s} → 正常返回，耗时 {time.time()-t0:.1f}s ✓")
    except Exception as e:
        print(f"  {说明:38s} → {type(e).__name__} @ {time.time()-t0:.1f}s ✗")
    await r.aclose()


async def main():
    print("实验：同一个 BLOCK=3000ms，改变 socket_timeout 会怎样？")
    print()
    await 测("socket_timeout=10s, block=3s", block_ms=3000, socket_timeout=10)
    await 测("socket_timeout=1s,  block=3s", block_ms=3000, socket_timeout=1)
    print()
    print("结论：")
    print("  socket_timeout 必须 **大于** BLOCK 时长，否则阻塞读会被提前打断。")
    print("  真实项目里：STREAM_SOCKET_TIMEOUT=30s，STREAM_BLOCK_MS=10000（留足余量）。")
    print()
    print("额外陷阱：redis 客户端对失败会**自动重试**（默认 3 次），")
    print("  所以一次 5s 的读超时，表现成约 15s 后才抛错——看起来像'莫名其妙断了'。")
    print("  这正是我项目里续收连接'静默断开'的真相。")


asyncio.run(main())
