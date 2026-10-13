"""ARQ worker：把「Agent 执行」从 API 进程搬到独立消费进程。

为什么要独立进程（原理）
------------------------
现在执行体是 API 进程内的 asyncio.Task：**进程一重启任务就消失**，事件日志里只留
半截。队列把「任务」变成 Redis 里的一条持久记录：

    生产端（API）  await enqueue_job("run_chat_task_job", **payload)
                     └─ arq 把函数名 + 参数序列化成 JSON，放进 Redis 的一个 list
    消费端（本文件）arq worker 进程 BRPOP 取任务 → 反序列化 → 调对应函数执行

收益：API 进程重启/发布不影响已入队的任务；worker 可水平扩容（同 job 只被一个
worker 取走）；白送重试、超时、死信。

与 B（事件日志）的分工
----------------------
- **B 解决「读者断线怎么接上」**：事件持久化成带编号的日志，按游标续读 → 事件不丢
- **A 解决「执行者挂了任务怎么办」**：任务持久化到队列，独立进程消费 → 任务不丢
两者组合才是生产级形态：**事件不丢 + 任务不丢**。

启动方式
--------
    .venv/Scripts/python.exe -m arq app.worker.WorkerSettings
（API 侧由 TASK_QUEUE_ENABLED 开关控制是否走队列，默认关闭＝沿用进程内执行）
"""
from __future__ import annotations

import logging

from arq.connections import RedisSettings, create_pool

from app.config import settings

logger = logging.getLogger("lvco.worker")

_pool = None


async def get_pool():
    """懒建 arq 连接池（生产端用；worker 进程自身不需要）。"""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def worker_alive() -> bool:
    """是否有 arq worker 在线。

    依据 arq 的心跳 key（WorkerSettings.health_check_interval=30 → TTL≈20s）。
    没有 worker 时入队只会让任务躺在队列里、前端一直转圈——所以宁可回退进程内执行。
    """
    try:
        pool = await get_pool()
        return bool(await pool.exists("arq:queue:health-check"))
    except Exception:  # noqa: BLE001
        return False


async def enqueue_chat_task(**payload) -> str | None:
    """把一轮对话任务投递到队列，返回 job_id。

    返回 None 的两种情况（调用方据此回退进程内执行）：
      1) 没有在线 worker —— 入队会变成"永远排队"，不如本进程直接跑；
      2) 入队本身失败（Redis 异常等）。
    """
    try:
        if not await worker_alive():
            logger.warning("no_arq_worker_online_fallback_inprocess")
            return None
        pool = await get_pool()
        job = await pool.enqueue_job("run_chat_task_job", **payload)
        if job is None:
            # 同 job_id 已存在（去重语义）——按已入队处理
            return "duplicate"
        logger.info("chat_task_enqueued job=%s session=%s", job.job_id, payload.get("session_id"))
        return job.job_id
    except Exception:  # noqa: BLE001
        logger.warning("chat_task_enqueue_failed", exc_info=True)
        return None


async def run_chat_task_job(
    ctx: dict,
    *,
    session_id: str | None,
    lock_token: str | None,
    assistant_msg_id: str | None,
    task_key: str | None,
    payload: dict,
) -> str:
    """队列消费端：在 worker 进程内重建上下文并执行一轮对话。

    payload 里是可序列化的「已装配上下文」（agent_message + 记忆 + 指标 + 窗口…）——
    由 API 侧在请求作用域内查好传入。这样 worker 不必重放一遍请求侧的取数逻辑，
    代价是 payload 稍大（几 KB，Redis 完全可承受）。
    """
    import uuid

    from app.services.agents.lead import LeadAgent, LeadContext
    from app.services.chat_task import run_chat_task
    from app.services.llm_client import LLMClient
    from app.services.observability import get_observer

    lead_ctx = LeadContext(
        user_id=payload.get("user_id") or "",
        session_id=payload.get("session_id") or "",
        entry=payload.get("entry") or "chat",
        datasource_id=payload.get("datasource_id"),
        history_summary=payload.get("history_summary") or "",
        memory_covered=int(payload.get("memory_covered") or 0),
        memory_watermark=payload.get("memory_watermark"),
        memory_fail_count=int(payload.get("memory_fail_count") or 0),
        extra_context=payload.get("extra_context") or "",
        metrics_ctx=payload.get("metrics_ctx") or "",
        ui_action=payload.get("ui_action"),
    )
    for turn in payload.get("turns") or []:
        if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
            lead_ctx.turns.append({"role": turn["role"], "content": str(turn.get("content", ""))})
    agent_message = payload.get("agent_message") or ""

    def event_factory(db_session):
        return LeadAgent(llm=LLMClient(settings), observer=get_observer()).stream(
            agent_message, ctx=lead_ctx, db_session=db_session
        )

    await run_chat_task(
        session_id=uuid.UUID(session_id) if session_id else None,
        lock_token=lock_token,
        assistant_msg_id=uuid.UUID(assistant_msg_id) if assistant_msg_id else None,
        task_key=task_key,
        event_factory=event_factory,
    )
    return "ok"


class WorkerSettings:
    """arq worker 配置：.venv/Scripts/python.exe -m arq app.worker.WorkerSettings"""

    functions = [run_chat_task_job]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10          # 单进程并发任务数
    job_timeout = 1800     # 单任务超时（秒）：分析类任务可能跑几分钟
    # 心跳间隔（秒）：arq 会周期性刷新 arq:queue:health-check（TTL = 间隔 - 10）。
    # 默认 3600s 会让"worker 是否在线"的判定迟钝一小时——缩短到 30s，
    # 使 API 侧能在 worker 挂掉后 ~20s 内察觉并回退进程内执行（见 enqueue_chat_task）。
    health_check_interval = 30
    keep_result = 300      # 结果保留时长（秒）
    max_tries = 1          # 不自动重试：LLM 任务重试会重复落块/重复计费，失败由事件流暴露给前端
