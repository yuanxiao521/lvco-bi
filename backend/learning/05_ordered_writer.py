# -*- coding: utf-8 -*-
"""第 5 课：为什么写日志要用"缓冲 + 单写入者"

事件是**按顺序产生**的（先生成"开始查询"，后生成"查询完成"），
但如果每个事件各自 create_task 去写 Redis，多个网络往返会**交错**，
日志里的顺序就可能和产生顺序不一致 —— 回放时就会看到错乱的流程。

跑法：.venv/Scripts/python.exe learning/05_ordered_writer.py
"""
import asyncio
import random

import redis.asyncio as aioredis

KEY_A = "learning:order:bad"
KEY_B = "learning:order:good"


async def main():
    r = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
    await r.delete(KEY_A, KEY_B)

    # 事件按 1..6 的顺序"产生"
    events = [f"事件{i}" for i in range(1, 7)]

    print("方式 A：每个事件各自 create_task 直接写（顺序会乱）")
    async def 直接写(ev, delay):
        await asyncio.sleep(delay)          # 模拟网络往返耗时不一
        await r.xadd(KEY_A, {"data": ev})
    random.seed(7)
    await asyncio.gather(*[直接写(ev, random.uniform(0, 0.05)) for ev in events])
    order_a = [f["data"] for _i, f in await r.xrange(KEY_A, "-", "+")]
    print(f"   产生顺序: {events}")
    print(f"   日志顺序: {order_a}")
    print(f"   一致吗？ {'是' if order_a == events else '否 ✗ —— 回放会错乱'}")

    print()
    print("方式 B：先入内存缓冲，单写入者按 FIFO 顺序写（顺序稳定）")
    buffer: list[str] = []
    stop = asyncio.Event()

    async def 单写入者():
        while True:
            if buffer:
                ev = buffer.pop(0)          # FIFO：先产生的先写
                await asyncio.sleep(0.01)   # 就算写入慢，也不会乱序
                await r.xadd(KEY_B, {"data": ev})
                continue
            if stop.is_set():
                return
            await asyncio.sleep(0.001)

    writer = asyncio.create_task(单写入者())

    def publish(ev):                        # 同步函数，和真实项目里的 _publish 一样
        buffer.append(ev)

    random.seed(7)
    for ev in events:
        publish(ev)
        await asyncio.sleep(random.uniform(0, 0.02))

    stop.set()
    await writer
    order_b = [f["data"] for _i, f in await r.xrange(KEY_B, "-", "+")]
    print(f"   产生顺序: {events}")
    print(f"   日志顺序: {order_b}")
    print(f"   一致吗？ {'是 ✓' if order_b == events else '否'}")

    await r.delete(KEY_A, KEY_B)
    await r.aclose()

    print()
    print("对照真实项目：")
    print("  _publish() 是同步函数（18 个调用点），它只做两件事：")
    print("    1) buffer.append(ev)              ← 保序缓冲")
    print("    2) chat_registry.publish(...)     ← 进程内广播（零延迟，给在线连接）")
    print("  另有唯一的 _log_writer 协程负责按序 XADD —— 保证日志顺序 = 事件产生顺序")


asyncio.run(main())
