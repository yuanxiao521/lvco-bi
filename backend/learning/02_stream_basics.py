# -*- coding: utf-8 -*-
"""第 2 课：Redis Stream 的五个基本命令（手把手）

跑法：.venv/Scripts/python.exe learning/02_stream_basics.py
（需要本地 Redis：docker 里的 lvco-redis，端口 6379）
"""
import asyncio

import redis.asyncio as aioredis

KEY = "learning:demo:stream"


async def main():
    # decode_responses=True → 返回字符串而不是 bytes（方便打印）
    r = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
    await r.delete(KEY)               # 清掉上次的残留，从零开始

    print("① XADD：追加一条事件，返回它的 ID")
    ids = []
    for i in range(1, 4):
        eid = await r.xadd(KEY, {"data": f"事件{i}"})
        ids.append(eid)
        print(f"   XADD → {eid}    （格式：毫秒时间戳-序号，天然递增有序）")

    print()
    print("② XLEN：日志长度")
    print(f"   XLEN → {await r.xlen(KEY)}")

    print()
    print("③ XRANGE：读一段（- 表示最小，+ 表示最大）")
    for eid, fields in await r.xrange(KEY, "-", "+"):
        print(f"   {eid} → {fields['data']}")

    print()
    print("④ XREAD：从某个 ID **之后**读（这是断线续传的核心）")
    items = await r.xread({KEY: "0"}, count=10)     # "0" = 从头读
    print(f"   从 0 读：{[f['data'] for _k, entries in items for _id, f in entries]}")
    items = await r.xread({KEY: ids[1]}, count=10)  # 从第 2 条的 ID 之后读
    print(f"   从 {ids[1]} 之后读：{[f['data'] for _k, entries in items for _id, f in entries]}")
    print("   ↑ 注意：不含 ids[1] 自己，只给「之后的」—— 游标语义天然不重复")

    print()
    print("⑤ MAXLEN：只保留最近 N 条（防止日志无限增长）")
    for i in range(100):
        # approximate=False → 精确裁剪（生产常用 approximate=True 换性能）
        await r.xadd(KEY, {"data": f"噪音{i}"}, maxlen=10, approximate=False)
    print(f"   写了 100 条噪音后，XLEN = {await r.xlen(KEY)}（被裁剪到 ~10）")

    await r.delete(KEY)
    await r.aclose()
    print()
    print("小结：XADD 追加 / XRANGE 全读 / XREAD 按游标读 / MAXLEN 限长")


asyncio.run(main())
