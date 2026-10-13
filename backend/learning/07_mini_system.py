# -*- coding: utf-8 -*-
"""第 7 课（结业）：把 B 和 A 串起来的最小系统

模拟真实架构的三个角色：
    [执行者] 跑任务，把每个事件写进 Stream        ← 对应 worker 进程
    [读 者] 按游标读 Stream，断线后能续上         ← 对应 API 的 SSE 转发
    [队 列] 任务先入队，由执行者取走              ← 对应 ARQ（这里用 Redis list 简化）

跑法：.venv/Scripts/python.exe learning/07_mini_system.py
"""
import asyncio
import json
import time

import redis.asyncio as aioredis

R = "redis://localhost:6379/0"
QUEUE = "learning:mini:queue"
EVENTS = "learning:mini:events"


async def 执行者(r, 任务名):
    """消费端：从队列取任务 → 跑 → 把事件写进 Stream（带 done 收尾）"""
    job = await r.blpop(QUEUE, timeout=5)
    if job is None:
        print("   [执行者] 队列空，退出")
        return
    print(f"   [执行者] 取到任务：{job[1]}")
    for i, 步骤 in enumerate(["查询数据", "生成图表", "校验数字", "写结论"], 1):
        await asyncio.sleep(0.25)
        ev = {"type": "progress", "index": i, "title": 步骤}
        await r.xadd(EVENTS, {"d": json.dumps(ev, ensure_ascii=False)})
        print(f"   [执行者] 写入事件：{步骤}")
    await r.xadd(EVENTS, {"d": json.dumps({"type": "done"}, ensure_ascii=False)})
    print("   [执行者] 写入 done，任务结束")


async def 读者(r, 游标, 最多等秒=3.0):
    """SSE 侧：从游标读历史，然后阻塞等新事件，直到 done"""
    收到 = []
    截止 = time.monotonic() + 最多等秒
    while time.monotonic() < 截止:
        items = await r.xread({EVENTS: 游标}, count=100, block=300)
        if not items:
            continue
        for _k, entries in items or []:
            for eid, f in entries:
                游标 = eid
                ev = json.loads(f["d"])
                收到.append(ev)
                if ev["type"] == "done":
                    return 收到, 游标
    return 收到, 游标


async def main():
    r = aioredis.from_url(R, decode_responses=True)
    await r.delete(QUEUE, EVENTS)

    print("场景 1：正常一轮（不断线）")
    await r.rpush(QUEUE, "生成季度分析报告")
    执行 = asyncio.create_task(执行者(r, "生成季度分析报告"))
    收到, 游标 = await 读者(r, "0", 最多等秒=4)
    await 执行
    print(f"   [读者] 收到 {len(收到)} 个事件，最后是 {收到[-1]['type']}")
    print(f"   [读者] 游标停在 {游标}")

    print()
    print("场景 2：读者读够 2 个事件就断线（模拟刷新），执行者继续跑")
    await r.delete(EVENTS)
    await r.rpush(QUEUE, "再来一轮")
    执行 = asyncio.create_task(执行者(r, "再来一轮"))

    部分, 游标2 = [], "0"
    while len(部分) < 2:                      # 明确"只读 2 个就断"
        items = await r.xread({EVENTS: 游标2}, count=10, block=500)
        for _k, entries in items or []:
            for eid, f in entries:
                游标2 = eid
                部分.append(json.loads(f["d"]))
    print(f"   [读者] 断线前收到 {len(部分)} 个，游标停在 {游标2}")
    await 执行                                 # 执行者不受影响，继续跑完 4 步 + done
    print("   [读者] 断线期间执行者把剩余事件都写进了日志 ✓")

    print()
    print("场景 3：重连（带上断线前的游标）")
    补收, 游标3 = await 读者(r, 游标2, 最多等秒=3)
    print(f"   [读者] 从游标续读，补收到 {len(补收)} 个事件：")
    for ev in 补收:
        print(f"        {ev.get('type')} {ev.get('title','')}")
    print(f"   → 断线前 {len(部分)} 个 + 补收 {len(补收)} 个 = 全部事件，无重复无遗漏 ✓")

    await r.delete(QUEUE, EVENTS)
    await r.aclose()

    print()
    print("=" * 56)
    print("真实项目的对应关系")
    print("=" * 56)
    print("  队列 QUEUE      → ARQ（app/worker.py: enqueue_chat_task / run_chat_task_job）")
    print("  执行者          → worker 进程（跑 LeadAgent，事件 XADD）")
    print("  事件日志 EVENTS → app/services/event_stream.py（publish / read_after / tail）")
    print("  读者            → API 的 SSE 转发（_stream_from_event_log）")
    print("  游标            → 前端重连时带上来的 lastEventId")
    print()
    print("两个独立的问题，两个独立的机制：")
    print("  B（事件日志）解决：读者断线怎么接上     → 事件不丢")
    print("  A（任务队列）解决：执行者挂了任务怎么办 → 任务不丢")


asyncio.run(main())
