# -*- coding: utf-8 -*-
"""第 3 课：游标续传——"刷新后一条不丢"的最小实现

场景：读者读到一半断线，生产者继续写；读者重连后从**自己记住的 ID** 之后读。
关键点：读者必须自己保存游标（真实项目里由前端保存，重连时带上来）。

跑法：.venv/Scripts/python.exe learning/03_cursor_resume.py
"""
import asyncio

import redis.asyncio as aioredis

KEY = "learning:cursor"


async def main():
    r = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
    await r.delete(KEY)

    async def 生产(text):
        eid = await r.xadd(KEY, {"data": text})
        print(f"   [生产] {text}  (id={eid})")
        return eid

    async def 读一段(游标):
        """从游标之后读；返回 (新事件列表, 新游标)"""
        items = await r.xread({KEY: 游标}, count=100)
        events, cursor = [], 游标
        for _k, entries in items or []:
            for eid, f in entries:
                events.append(f["data"])
                cursor = eid          # ← 每读一条就推进游标
        return events, cursor

    print("--- 第一段：读者在线 ---")
    await 生产("进度:查询数据")
    await 生产("进度:生成图表")
    got, cursor = await 读一段("0")
    print(f"   [读者] 收到 {got}，游标停在 {cursor}")

    print()
    print("--- 读者断线（模拟浏览器刷新）---")
    await 生产("进度:校验数字")
    await 生产("完成:报告已生成")
    print("   [读者] 不在线，但这 2 条仍在日志里 ✓")

    print()
    print("--- 重连：带上刚才的游标 ---")
    got, cursor = await 读一段(cursor)
    print(f"   [读者] 补收到 {got}")
    print("   ↑ 只拿到断线期间的 2 条：不重复、不丢失")

    print()
    print("--- 再读一次（游标已到末尾）---")
    got, cursor = await 读一段(cursor)
    print(f"   [读者] 收到 {got}（空）—— 因为游标之后没有新事件了")

    await r.delete(KEY)
    await r.aclose()

    print()
    print("对照真实项目：")
    print("  生产 = _run_chat_task 里 event_stream.publish(ev)（XADD）")
    print("  读   = 续收分支 event_stream.read_after(task_sid, cursor)")
    print("  游标 = 前端重连时带上的 lastEventId")


asyncio.run(main())
