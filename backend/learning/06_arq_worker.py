# -*- coding: utf-8 -*-
"""第 6 课（上）：ARQ worker —— 消费端

这是**另一个进程**要跑的文件。它定义"我能处理哪些任务"。

启动：.venv/Scripts/python.exe -m arq learning.06_arq_worker.WorkerSettings
（注意：arq 用模块路径，所以要能 import 到，即从 backend 目录跑）
"""
import asyncio

from arq.connections import RedisSettings


async def 打招呼(ctx: dict, *, 名字: str, 睡几秒: float = 0.5) -> str:
    """一个最简任务：睡一会儿，返回一句话。

    真实项目里这个函数是 run_chat_task_job —— 它重建 LeadContext 并调用
    与 API 进程共用的执行内核 run_chat_task。
    """
    print(f"   [worker] 收到任务：名字={名字}（第 {ctx['job_try']} 次尝试）")
    await asyncio.sleep(睡几秒)
    print(f"   [worker] 任务完成：{名字}")
    return f"你好，{名字}！"


class WorkerSettings:
    """arq 读取这个类来决定：处理哪些函数、连哪个 Redis、并发多少。"""

    functions = [打招呼]
    redis_settings = RedisSettings.from_dsn("redis://localhost:6379/0")
    max_jobs = 5
    job_timeout = 60
    # 心跳：arq 会周期性刷新 arq:queue:health-check，供"worker 是否在线"判定
    health_check_interval = 30
