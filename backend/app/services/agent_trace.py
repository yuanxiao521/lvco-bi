# -*- coding: utf-8 -*-
"""agent_calls 采集器（P1-C B1-lite）：同步入队 + 单写入者批量落库。

为什么是"缓冲 + 单写入者"
------------------------
工具调用/LLM 调用都在**请求主链路**上。写库（网络往返）绝不能阻塞它们，
也不能每个调用点各自 create_task 去写（并发交错、连接池打爆、顺序错乱）。
所以：record() 只做一次 dict 复制 + 入队（同步、零 await、绝不抛），
一个后台单写入者批量 INSERT；chat_task 收尾时再 await flush() 兜底。

开关
----
AGENT_CALL_LOG=off  → 不投递（测试④）；默认 full = 全量。
（"失败行无条件记录"：在 full/sampled 模式下失败行永不被采样丢弃；
  queue 满时仍可能丢，会体现在 stats.dropped 里。off 按测试要求=零投递。）

上下文
------
trace_id / turn_id / entry / session_id / message_id / user_id 放在 contextvars：
入口（api/v1/ai.py）绑定一次，asyncio 子任务自动继承；队列模式（worker 进程）
由 chat_task 在任务开头重新绑定（跨进程 contextvars 传不过去）。

parent（P4-B）
-------------
parent 表示"这条记录属于哪一层的调用"（lead / dispatch / agent_orch / canvas_orch /
react / recon）。用 parent_scope() 的**进入/退出还原**语义实现：编排器在自己的
执行入口 set，退出时 reset，绝不泄漏到下一步或下一次调用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from app.config import settings

logger = logging.getLogger("lvco.agent_trace")

# ── 队列与统计 ──────────────────────────────────────────────────────────────
_QUEUE_MAX = 2000                      # 内存上界：超了丢新事件并计数（不拖慢主链路）
_queue: deque[dict] = deque()
_stats: dict[str, int] = {
    "enqueued": 0,
    "written": 0,
    "dropped": 0,          # 队列满丢弃
    "skipped_unbound": 0,  # 没有 trace 上下文（脚本/未接入的路径）
    "record_errors": 0,
    "flush_errors": 0,
}
_autoflush = True                      # 测试可关掉后台写入，走显式 flush()
_flusher_task: asyncio.Task | None = None
_flusher_loop: asyncio.AbstractEventLoop | None = None
_flush_lock: asyncio.Lock | None = None
_flush_lock_loop: asyncio.AbstractEventLoop | None = None

# ── 上下文 ──────────────────────────────────────────────────────────────────
_UNSET = object()
_trace_var: ContextVar[str | None] = ContextVar("agent_trace_id", default=None)
_turn_var: ContextVar[str | None] = ContextVar("agent_turn_id", default=None)
_seq_var: ContextVar[dict | None] = ContextVar("agent_trace_seq", default=None)
_entry_var: ContextVar[str | None] = ContextVar("agent_trace_entry", default=None)
_session_var: ContextVar[Any] = ContextVar("agent_trace_session", default=None)
_message_var: ContextVar[Any] = ContextVar("agent_trace_message", default=None)
_user_var: ContextVar[Any] = ContextVar("agent_trace_user", default=None)
# P4-B：调用归属（lead / dispatch / agent_orch / canvas_orch / recon）
_parent_var: ContextVar[str | None] = ContextVar("agent_trace_parent", default=None)


def log_mode() -> str:
    """当前采集档位：off / full（sampled 预留，当前未实现）。"""
    raw = str(getattr(settings, "AGENT_CALL_LOG", "full") or "full").strip().lower()
    return raw if raw in ("off", "full", "sampled") else "full"


def enabled() -> bool:
    return log_mode() != "off"


def stats() -> dict[str, int]:
    """采集统计（排障用：丢了多少、写了多少）。"""
    return dict(_stats)


# ── 绑定上下文 ──────────────────────────────────────────────────────────────
def _new_id() -> str:
    return str(uuid.uuid4())


def bind(
    *,
    entry: str | None = None,
    user_id: Any = _UNSET,
    session_id: Any = _UNSET,
    message_id: Any = _UNSET,
    trace_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """绑定本轮的 trace/turn 上下文（入口调用一次，子任务自动继承）。

    - trace_id/turn_id 未提供且尚未绑定时自动生成（队列模式下 chat_task 用）；
    - session_id/message_id/user_id 只在**显式传入**时覆盖（None 可显式清除）。
    """
    try:
        if trace_id is not None:
            _trace_var.set(str(trace_id))
            _seq_var.set({"n": 0})
        elif _trace_var.get() is None:
            _trace_var.set(_new_id())
            _seq_var.set({"n": 0})

        if turn_id is not None:
            _turn_var.set(str(turn_id))
        elif _turn_var.get() is None:
            _turn_var.set(_new_id())

        if entry:
            _entry_var.set(str(entry))
        if user_id is not _UNSET:
            _user_var.set(user_id)
        if session_id is not _UNSET:
            _session_var.set(session_id)
        if message_id is not _UNSET:
            _message_var.set(message_id)
    except Exception:  # noqa: BLE001
        logger.debug("[agent_trace] bind_failed", exc_info=True)


def current_trace_id() -> str | None:
    return _trace_var.get()


def current_turn_id() -> str | None:
    return _turn_var.get()


def current_parent() -> str | None:
    """当前调用归属（供测试/排障；记录时由 record() 自动带上）。"""
    return _parent_var.get()


def set_parent(parent: str | None) -> None:
    """设置当前调用的 parent（**不还原**：用于"本任务内默认 parent"的粗粒度场景）。

    精细场景请用 parent_scope()，它会进入/退出还原，不会泄漏到下一步。
    """
    try:
        _parent_var.set(str(parent) if parent else None)
    except Exception:  # noqa: BLE001
        logger.debug("[agent_trace] set_parent_failed", exc_info=True)


@contextmanager
def parent_scope(parent: str):
    """parent 的父子作用域：进入 set、退出**还原**到外层值（异常也还原）。

    用法：
        with agent_trace.parent_scope("agent_orch"):
            await graph.invoke(...)      # 其间所有 tool/llm 记录 parent=agent_orch
    """
    token = _parent_var.set(str(parent))
    try:
        yield
    finally:
        try:
            _parent_var.reset(token)
        except Exception:  # noqa: BLE001
            # 跨 context 误用（理论上不会）时兜底，绝不让可观测把主链路搞挂
            _parent_var.set(None)


def _next_seq() -> int:
    counter = _seq_var.get()
    if counter is None:
        counter = {"n": 0}
        _seq_var.set(counter)
    n = int(counter.get("n", 0))
    counter["n"] = n + 1
    return n


# ── 采集入口（同步、零 await、绝不抛）──────────────────────────────────────
def record(ev: dict) -> None:
    """记录一条调用事件。失败/异常一律吞掉——可观测不能把主链路搞挂。"""
    if not enabled():
        return
    try:
        trace_id = _trace_var.get()
        if not trace_id:
            _stats["skipped_unbound"] += 1
            return
        if len(_queue) >= _QUEUE_MAX:
            _stats["dropped"] += 1
            return
        item = dict(ev or {})
        item["trace_id"] = trace_id
        item.setdefault("turn_id", _turn_var.get())
        item.setdefault("entry", _entry_var.get() or "chat")
        item.setdefault("session_id", _session_var.get())
        item.setdefault("message_id", _message_var.get())
        item.setdefault("user_id", _user_var.get())
        item.setdefault("parent", _parent_var.get())
        item["seq"] = _next_seq()
        _queue.append(item)
        _stats["enqueued"] += 1
        if _autoflush:
            _ensure_flusher()
    except Exception:  # noqa: BLE001
        _stats["record_errors"] += 1


def _ensure_flusher() -> None:
    """确保当前事件循环里有一个后台单写入者（跨测试/多 loop 时按 loop 重建）。"""
    global _flusher_task, _flusher_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 无事件循环（脚本/同步测试）→ 由调用方显式 flush()
    if _flusher_task is not None and not _flusher_task.done() and _flusher_loop is loop:
        return
    try:
        _flusher_loop = loop
        _flusher_task = loop.create_task(_flusher(), name="agent-calls-flusher")
    except Exception:  # noqa: BLE001
        logger.debug("[agent_trace] flusher_start_failed", exc_info=True)


async def _flusher() -> None:
    """单写入者：每 200ms 把队列里的记录批量落库一次（异常只记日志，绝不退出）。"""
    while True:
        try:
            await asyncio.sleep(0.2)
            if _queue:
                await flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("[agent_trace] flusher_loop_error", exc_info=True)


# ── 脱敏 ────────────────────────────────────────────────────────────────────
# 只放行这些键（其余值统一 "<masked>"）：都是"排障需要、且本身不含秘密"的参数。
_ALLOWED_ARG_KEYS = frozenset({
    "datasource_id", "canvas_id", "sql", "goal", "gap", "text", "question",
    "ask_kind", "complexity", "intent", "reason", "acceptance", "constraints",
    "keywords", "limit", "chart_type", "block_type", "block_id", "title",
    "dimensions", "measures", "filters", "fields", "sort", "purpose",
    # P4-B：dispatch 行的结构化摘要（success/elapsed_ms 走 ok/duration_ms 列）
    "blocks_added", "mode",
})
_MASKED = "<masked>"
_ARGS_MAX_CHARS = 2048
_PREVIEW_MAX_CHARS = 500
_ERROR_MAX_CHARS = 500


def _mask_sql(sql: str) -> str:
    """把 SQL 里的字面量换成 ?（sqlglot 失败退化为正则）。"""
    if not isinstance(sql, str) or not sql.strip():
        return sql if isinstance(sql, str) else ""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql)
        if tree is None:
            raise ValueError("parse_none")
        for literal in list(tree.find_all(exp.Literal)):
            literal.replace(exp.Placeholder())
        return tree.sql()
    except Exception:  # noqa: BLE001
        try:
            return re.sub(r"'(?:[^']|'')*'", "'?'", sql)
        except Exception:  # noqa: BLE001
            return sql


def _redact_args(args: Any, *, blocked_columns: list[str] | None = None) -> dict | None:
    """参数脱敏：白名单键 + SQL 字面量替换；敏感列命中时保留线索。"""
    args_dict = args if isinstance(args, dict) else {}
    masked_sql = _mask_sql(str(args_dict.get("sql") or "")) if args_dict.get("sql") else ""

    if blocked_columns:
        # 敏感列：不要整个 omit —— 排障需要知道"被哪几列拦了、想查什么"
        out = {"blocked_columns": [str(c) for c in blocked_columns][:20]}
        if masked_sql:
            out["sql_masked"] = masked_sql
        return out

    if not args_dict:
        return None

    redacted: dict[str, Any] = {}
    for key, value in args.items():
        if key not in _ALLOWED_ARG_KEYS:
            redacted[key] = _MASKED
            continue
        if key == "sql":
            redacted[key] = masked_sql
        else:
            try:
                json.dumps(value, ensure_ascii=False, default=str)
                redacted[key] = value
            except Exception:  # noqa: BLE001
                redacted[key] = _MASKED

    text = json.dumps(redacted, ensure_ascii=False, default=str)
    if len(text) > _ARGS_MAX_CHARS:
        return {"_truncated": True, "keys": sorted(redacted), "_chars": len(text)}
    return redacted


def _as_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _truncate(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _prepare_row(ev: dict) -> dict | None:
    """把内部事件 dict 变成 agent_calls 的列 dict（列名与模型对齐）。"""
    trace_id = _as_uuid(ev.get("trace_id"))
    if trace_id is None:
        return None
    blocked = ev.get("blocked_columns")
    return {
        "trace_id": trace_id,
        "turn_id": _as_uuid(ev.get("turn_id")),
        "seq": int(ev.get("seq") or 0),
        "kind": str(ev.get("kind") or "event")[:12],
        "entry": str(ev.get("entry") or "chat")[:12],
        "mode": _truncate(
            ev.get("mode") or getattr(settings, "LEAD_AGENT_MODE", "legacy"), 24
        ),
        "parent": _truncate(ev.get("parent"), 24),
        "session_id": _as_uuid(ev.get("session_id")),
        "message_id": _as_uuid(ev.get("message_id")),
        "user_id": _as_uuid(ev.get("user_id")),
        "tool_name": _truncate(ev.get("tool_name"), 64),
        "tool_call_id": _truncate(ev.get("tool_call_id"), 64),
        "args_json": _redact_args(ev.get("args"), blocked_columns=blocked),
        "args_hash": _truncate(ev.get("args_hash"), 8),
        "ok": ev.get("ok"),
        "error_kind": _truncate(ev.get("error_kind"), 32),
        "error_layer": ev.get("error_layer"),
        "error_msg": _truncate(ev.get("error_msg"), _ERROR_MAX_CHARS),
        "result_chars": ev.get("result_chars"),
        "result_preview": _truncate(ev.get("result_preview"), _PREVIEW_MAX_CHARS),
        "duration_ms": ev.get("duration_ms"),
        "attempt": ev.get("attempt"),
        "memo_hit": ev.get("memo_hit"),
        "model": _truncate(ev.get("model"), 64),
        "tokens_in": ev.get("tokens_in"),
        "tokens_out": ev.get("tokens_out"),
        "tokens_cached": ev.get("tokens_cached"),
        "degraded": ev.get("degraded"),
        "rounds": ev.get("rounds"),
        "tool_calls": ev.get("tool_calls"),
        "tool_failures": ev.get("tool_failures"),
    }


# ── 写入 ────────────────────────────────────────────────────────────────────
async def _insert(db, rows: list[dict]) -> None:
    from app.models.agent_call import AgentCall

    db.add_all([AgentCall(**row) for row in rows])
    await db.commit()


async def _write_batch(rows: list[dict], db=None) -> None:
    """单批落库（测试里被 monkeypatch 成内存收集器）。

    db 为 None 时自建短会话（后台单写入者用）；chat_task 收尾时传入它自己的会话，
    避免每轮多开一条连接，也让单测的假 session 能兜住（写失败只记日志）。
    """
    if not rows:
        return
    if db is not None:
        try:
            await _insert(db, rows)
        except Exception:
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        return
    from app.core.database import async_session_factory

    async with async_session_factory() as owned:
        try:
            await _insert(owned, rows)
        except Exception:
            await owned.rollback()
            raise


async def flush(db=None) -> int:
    """把当前队列全部落库，返回写入行数（绝不抛）。db 可选（chat_task 传自己的会话）。"""
    global _flush_lock, _flush_lock_loop
    rows: list[dict] = []
    while _queue:
        try:
            rows.append(_queue.popleft())
        except IndexError:  # pragma: no cover - 并发弹出的兜底
            break
    if not rows:
        return 0

    prepared: list[dict] = []
    for ev in rows:
        try:
            row = _prepare_row(ev)
        except Exception:  # noqa: BLE001
            _stats["record_errors"] += 1
            continue
        if row is not None:
            prepared.append(row)
    if not prepared:
        return 0

    try:
        loop = asyncio.get_running_loop()
        if _flush_lock is None or _flush_lock_loop is not loop:
            _flush_lock = asyncio.Lock()
            _flush_lock_loop = loop
        async with _flush_lock:
            await _write_batch(prepared, db=db)
        _stats["written"] += len(prepared)
        return len(prepared)
    except Exception:  # noqa: BLE001
        _stats["flush_errors"] += 1
        logger.warning("[agent_trace] flush_failed rows=%d", len(prepared), exc_info=True)
        return 0


# ── 消息级步骤投影（UI 物化视图；事实源仍是本表）─────────────────────────

# FeedStep 对齐前端 ActivityFeed：{id, kind, agent, toolName, ok, durationMs, summary}
# 只投影 tool / dispatch：用户要看的"做了什么"是**动作**（发了什么工具调用），
# LLM 推理不是动作——一行一次调用会把真正的工具动作淹掉，且模型名/tokens 对
# 排查"Agent 到底调了什么"没有信息增量。turn 汇总行同样不进投影。
_PROJECTION_KIND_MAP = {"tool": "tool", "dispatch": "dispatch"}


def _agent_of(parent: Any, kind: str) -> str:
    """投影里的归属标签：dispatch 恒为 Lead；工具行取 trace 的 parent。

    parent 由 agent_trace.parent_scope 维护（recon / agent_orch / canvas_orch / react）；
    Lead 自己直连工具时 parent 仍是 lead（stream_unified 入口 set_parent("lead")）——
    注意 parent=None 只会在作用域缺失时出现，那种情况归到 lead 会把 worker 的
    工具误报成 Lead 调的，所以每个 worker 内核都必须自己开作用域（见 lead_tools
    react 分支、canvas_orch/agent_orch 的 _run_step_with_timeout）。
    """
    if kind == "dispatch":
        return "lead"
    value = str(parent or "").strip()
    return value or "lead"


async def generate_steps_projection(db: Any, trace_id: str | None) -> list[dict]:
    """把一轮的 agent_calls 明细生成前端 ActivityFeed 形状的步骤数组。

    事实源是本表（agent_calls）；生成结果随 assistant 消息落库（ai_messages.steps），
    历史消息展开过程时零查询成本，且不受明细 30 天清理影响。
    trace_id 为空或查询失败 → 返回 []（投影是附加能力，绝不阻断收尾）。
    """
    if not trace_id:
        return []
    try:
        from sqlalchemy import select as _select

        from app.models.agent_call import AgentCall

        # 排序不能用 seq：seq 是 ContextVar 计数，Lead（派发）与内核（工具/LLM）
        # 在不同异步上下文各自从 0 计数，交错后 seq ≠ 真实时序（实测 dispatch 排到了
        # 全部工具之后）。id 是入库自增，同批 flush 内顺序稳定，且 dispatch 与
        # 工具分属不同批次 → id 全局单调。dispatch 语义上先于内核执行，再前置一层。
        rows = (
            await db.execute(
                _select(AgentCall)
                .where(
                    AgentCall.trace_id == str(trace_id),
                    AgentCall.kind.in_(tuple(_PROJECTION_KIND_MAP)),
                )
                .order_by(AgentCall.id.asc())
            )
        ).scalars().all()
        rows = sorted(
            rows,
            key=lambda r: (
                0 if str(r.kind) == "dispatch" else 1,  # 派发动作在最前
                int(r.id),
            ),
        )
        steps: list[dict] = []
        for i, r in enumerate(rows, 1):
            kind = str(r.kind)
            if kind not in _PROJECTION_KIND_MAP:
                continue  # 双保险：turn 汇总行不进投影（SQL 已过滤，这里防将来查询被改）
            step: dict = {
                "id": f"{kind}-{i}",
                "kind": _PROJECTION_KIND_MAP.get(kind, kind),
                "agent": _agent_of(getattr(r, "parent", None), kind),
                "toolName": str(getattr(r, "tool_name", "") or ""),
                "ok": bool(getattr(r, "ok", True)),
                "durationMs": int(getattr(r, "duration_ms", 0) or 0),
                "summary": str(getattr(r, "result_preview", "") or "")[:120],
                # 与直播同等信息量：展开能看到完整参数（脱敏后的 args_json）与结果预览
                "args": getattr(r, "args_json", None) or {},
                "result": str(getattr(r, "result_preview", "") or "")[:400],
            }
            if getattr(r, "error_kind", None):
                step["errorKind"] = str(r.error_kind)
            if getattr(r, "error_msg", None):
                step["errorMsg"] = str(r.error_msg)[:200]
            steps.append(step)
        return steps
    except Exception:  # noqa: BLE001
        logger.warning("[agent_trace] steps_projection_failed", exc_info=True)
        return []


# ── 保留策略（不建调度器，只提供函数）─────────────────────────────────────
async def purge_old(older_than_days: int | None = None) -> int:
    """删除过期的**明细行**（tool/llm），turn 汇总行保留；返回删除行数。

    挂法（三选一，均不需要改本模块）：
      1) 手动/cron：python -c "import asyncio;from app.services.agent_trace import purge_old;print(asyncio.run(purge_old()))"
      2) 现有 insight 调度器里加一个每日 job；
      3) ARQ worker 的 cron 任务。
    """
    from datetime import datetime, timedelta, timezone

    days = int(
        older_than_days
        if older_than_days is not None
        else getattr(settings, "AGENT_CALL_RETAIN_DAYS", 30) or 30
    )
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        from sqlalchemy import delete

        from app.core.database import async_session_factory
        from app.models.agent_call import AgentCall

        async with async_session_factory() as db:
            result = await db.execute(
                delete(AgentCall).where(
                    AgentCall.kind != "turn", AgentCall.created_at < cutoff
                )
            )
            await db.commit()
            return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001
        logger.warning("[agent_trace] purge_failed days=%s", days, exc_info=True)
        return 0


# ── 测试辅助 ────────────────────────────────────────────────────────────────
def reset() -> None:
    """清空队列/统计/上下文（测试用；生产不建议调用）。"""
    global _flusher_task, _flusher_loop, _autoflush, _flush_lock, _flush_lock_loop
    _queue.clear()
    for key in _stats:
        _stats[key] = 0
    _trace_var.set(None)
    _turn_var.set(None)
    _seq_var.set(None)
    _entry_var.set(None)
    _session_var.set(None)
    _message_var.set(None)
    _user_var.set(None)
    _parent_var.set(None)
    if _flusher_task is not None and not _flusher_task.done():
        try:
            _flusher_task.cancel()
        except Exception:  # noqa: BLE001
            pass
    _flusher_task = None
    _flusher_loop = None
    _autoflush = True
    _flush_lock = None
    _flush_lock_loop = None
