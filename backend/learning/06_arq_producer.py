# -*- coding: utf-8 -*-
"""第 6 课（下）：ARQ 生产者 —— 投递任务

跑法（两个终端）：
    终端 1：.venv/Scripts/python.exe -m arq learning.06_arq_worker.WorkerSettings
    终端 2：.venv/Scripts/python.exe learning/06_arq_producer.py
"""
import asyncio

from arq.connections import RedisSettings, create_pool


async def main():
    pool = await create_pool(RedisSettings.from_dsn("redis://localhost:6379/0"))

    # ① 检查有没有 worker 在线（真实项目用它做兜底：没 worker 就回退本进程执行）
    alive = await pool.exists("arq:queue:health-check")
    print(f"① worker 在线？ {bool(alive)}")
    if not alive:
        print("   请先在另一个终端启动 worker，再跑本脚本。")
        await pool.aclose()
        return

    # ② 投递任务：函数名 + 关键字参数（会被序列化成 JSON 放进 Redis）
    job = await pool.enqueue_job("打招呼", 名字="小明", 睡几秒=1.0)
    print(f"② 已投递 job_id = {job.job_id}")
    print("   （此刻 API 进程可以立刻返回，任务由 worker 慢慢跑）")

    # ③ 等结果（真实项目不需要等——它是靠事件日志给前端推流）
    result = await job.result(timeout=30)
    print(f"③ 任务结果：{result}")

    await pool.aclose()

    print()
    print("对照真实项目：")
    print("  投递 = enqueue_chat_task(payload={agent_message, 记忆, 窗口...})")
    print("  不等结果：任务把事件写进 Redis Stream，SSE 侧按游标读（这就是 B）")
    print("  在线检查 = worker_alive() → 没有 worker 就回退进程内执行，避免前端永远转圈")


asyncio.run(main())
