"""对话后台任务内核（从 API 模块抽出，供 API 进程与 worker 进程共用）。

为什么要独立成模块
------------------
原实现把「Agent 执行 + 事件广播 + 落库占位行 + 释放会话锁」整体写在 API 请求的
闭包里，只有**同一个进程**能跑。抽出后，队列的消费端（另一个进程）也能 import
到这份内核——这是把执行搬上任务队列（ARQ）的前置条件。

职责
----
消费 Agent 事件流 → 广播给订阅者（进程内注册表，供本进程连接实时转发）
→ 追加到事件日志（Redis Stream，供跨进程/断线回放）
→ 收尾：冲刷日志、落库 assistant 内容、清任务态、释放会话锁。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_cache_repository
from app.config import settings
from app.core.database import async_session_factory
from app.models.ai_message import AIMessage
from app.services.chat_stream_registry import chat_registry
from app.services.event_stream import event_stream
from app.services.llm_client import AINotConfiguredError, AIUpstreamError
from app.services.session_lock import release_session_lock

logger = logging.getLogger("lvco.services.chat_task")
_log = logger
CHAT_STREAM_TEXT_PREFIX = "ai:chat:stream:"
STREAM_TEXT_SNAPSHOT_INTERVAL = 0.5
STREAM_IDLE_TIMEOUT = 90.0


async def lead_stream_guard(lead_stream):
    """主导 Agent 流式消费护栏（执行内核的一部分，API 进程与 worker 共用）。

    主导 Agent 是唯一执行路径——异常时**不静默降级**，而是补发 status + done(degraded=True)，
    让失败被看见。

    为什么必须两端共用：队列模式下 agent 异常若没被转成 done 事件，事件日志里就永远
    没有终止信号，SSE 侧会一直阻塞续读到超时（前端表现：永远转圈）。
    """
    emitted = False
    try:
        async for ev in lead_stream:
            emitted = True
            yield ev
    except Exception as exc:  # noqa: BLE001
        _log.exception(f"lead_agent_stream_failed: {exc}")
        message = (
            "主导 Agent 执行异常，本轮已中断。" if emitted
            else "主导 Agent 不可用，本轮未能处理。"
        )
        yield {"type": "status", "message": message, "degradation": "lead_agent_error"}
        yield {"type": "done", "degraded": True}


async def _save_memory(db: AsyncSession, session_id, event: dict) -> None:
    """持久化记忆合并事件（消费主导 Agent 的 compressed_history 事件）。

    **来源守卫**：主导 Agent 路径的事件带 `source="lead"`，按消息级水位记账。
    非 lead 来源一律拒绝写入——防止任何旁路把累积长期记忆冲回旧语义（历史遗留的
    第二套 covered_rounds 口径已随旧路径一并移除，此守卫保留作防御）。

    三类事件：
    - 正常合并：写摘要 + 覆盖进度 + 推进水位 + 成功计数 +1、失败计数清零；
    - 失败（`failed`）：只把连续失败计数 +1（供熔断），不动摘要与水位；
    - 仅推进（无 summary 但有水位）：本段没有可并入内容，只把水位/进度向前推。
    """
    if not session_id:
        return
    try:
        from app.repositories.ai_session_repository import SQLAlchemyAIMemoryRepository
        repo = SQLAlchemyAIMemoryRepository(db)

        if event.get("source") != "lead":
            _log.info("skip_non_lead_memory_write source=%s", event.get("source"))
            return

        if event.get("failed"):
            await repo.bump_merge_failure(session_id)
            await db.commit()
            return

        covered = int(event.get("covered_rounds") or 0)
        watermark = None
        raw_wm = event.get("last_merged_message_id")
        if raw_wm:
            try:
                watermark = uuid.UUID(str(raw_wm))
            except (ValueError, TypeError):
                watermark = None

        summary = (event.get("summary") or "").strip()
        if not summary:
            if watermark is not None:
                await repo.set_progress(
                    session_id, covered_rounds=covered, last_merged_message_id=watermark
                )
                await db.commit()
            return
        await repo.upsert(
            session_id, summary, covered_rounds=covered, last_merged_message_id=watermark
        )
        await db.commit()
    except Exception:
        _log.warning("save_ai_memory_failed", exc_info=True)



async def _idle_guard(source, *, timeout: float = STREAM_IDLE_TIMEOUT, label: str = "stream"):
    """给事件流套空闲看门狗：超时无事件则补 error + done 强制收尾。

    没有它时，Agent 内部一旦停在某个 await（队列哨兵丢失 / 上游流不关），
    SSE 会永久挂着：前端一直"思考中"，后端一条错误日志都没有（因为确实没报错）。
    命中时打 warning（含 label 便于定位是哪一段），并保证客户端总能收到终止事件。
    """
    aiter = source.__aiter__()
    last_type = "<start>"
    last_at = time.monotonic()
    while True:
        try:
            ev = await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            # 关键诊断信息：卡住前最后收到的事件（定位到具体阶段）+ 静默时长
            _log.warning(
                "stream idle timeout label=%s timeout=%ss last_event=%s silent=%.1fs "
                "→ force close (agent parked)",
                label, int(timeout), last_type, time.monotonic() - last_at,
            )
            yield {"type": "error", "message": f"响应超时（{int(timeout)} 秒无输出），已中断本轮，请重试。"}
            yield {"type": "done", "degraded": True}
            return
        if isinstance(ev, dict) and ev.get("type"):
            last_type = str(ev.get("type"))
        last_at = time.monotonic()
        yield ev


# ── 对话后台续跑：Redis 文本快照 ──────────────────────────────────

def _chat_text_key(session_id: object) -> str:
    return f"{CHAT_STREAM_TEXT_PREFIX}{session_id}"



def _write_streamed_text(session_id: object, content: str) -> None:
    """写入文本快照：刷新/断线重连时补发已产出内容（进程重启也能兜底捞回）。"""
    try:
        get_cache_repository().set(_chat_text_key(session_id), content, ttl=settings.redis_ttl)
    except Exception:
        _log.debug("chat streamed text cache set failed", exc_info=True)



async def run_chat_task(
    *,
    session_id: str | None,
    lock_token: str | None,
    assistant_msg_id: Any | None,
    task_key: str | None,
    event_factory: Any,
) -> None:
    """后台任务主体（以独立 asyncio.Task 运行，连接断开不影响它）。

    不能复用请求的 db session：客户端断开后 FastAPI 的 get_db 会把它关掉。
    这里用 async_session_factory() 自建会话，负责：消费 Agent 事件 → 广播给订阅者 →
    更新 Redis 文本快照 → 落库占位行 → 清任务态 → 释放会话锁。

    事件的文本处理（代码块剥离、状态行过滤、全量累积）从"每个连接"上移到任务级，
    保证多个订阅者看到完全一致的结果，且刷新后能拿到完整文本。
    """
    full_content = ""
    collected_charts: list[dict] = []
    seen_chart_keys: set[str] = set()
    _fence_state = "closed"
    _fence_buffer = ""
    last_redis_write = 0.0
    completed = False

    # === AI 状态文字过滤器（原 event_generator 内实现，原样搬移） ===
    _STATUS_EMOJI_RE = re.compile(r'^[📂✅🔍📊📦⚠️]\s')
    _STATUS_KEYWORDS_RE = re.compile(r'正在|已找到|查询成功|查询失败|图表生成|图表已|数据源|浏览|执行')
    _META_START_RE = re.compile(r'^(?:好的|太好了)[！!]')
    _META_KEYWORDS_RE = re.compile(r'查看|连接|分析|生成|拿到|拉取|查询|扫描|数据源|数据已经|数据结构|开始|报告|图表')
    _META_SELF_RE = re.compile(r'^(?:我先|让我)')
    _META_SELF_KEYWORDS_RE = re.compile(r'查询|看看|拉取|获取|扫描|分析一下|预览')

    def _filter_status_text(text: str) -> str:
        if not text:
            return text
        lines = text.split('\n')
        filtered: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                filtered.append(line)
                continue
            content = stripped
            if content.startswith('> '):
                content = content[2:].strip()
            if _STATUS_EMOJI_RE.match(content) and _STATUS_KEYWORDS_RE.search(content):
                continue
            if _META_START_RE.match(content) and _META_KEYWORDS_RE.search(content):
                continue
            if _META_SELF_RE.match(content) and _META_SELF_KEYWORDS_RE.search(content):
                continue
            filtered.append(line)
        result = '\n'.join(filtered)
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result

    def _filter_canvas_tool_result_lines(text: str) -> str:
        if not text:
            return text
        lines = text.split('\n')
        filtered: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                filtered.append(line)
                continue
            if re.match(r'^已添加(?:文本|图表|块)[：:]\s*', stripped):
                continue
            if re.match(r'^分析完成[，,]\s*已在画布上添加了 \d+ 个内容块', stripped):
                continue
            filtered.append(line)
        result = '\n'.join(filtered)
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result

    def _chart_key(c: dict) -> str:
        return f"{c.get('chart_type', '?')}::{json.dumps(c.get('option', {}), ensure_ascii=False, sort_keys=True)}"

    def _strip_and_normalize(delta: str) -> str:
        """剥离 ```...``` 代码块并升级单 \\n 为 \\n\\n；返回可显示 delta（累积进 full_content）。"""
        nonlocal _fence_state, _fence_buffer, full_content
        out_parts: list[str] = []
        i = 0
        n = len(delta)
        while i < n:
            ch = delta[i]
            if _fence_state == "open":
                _fence_buffer += ch
                if len(_fence_buffer) >= 3 and _fence_buffer.endswith("```"):
                    _fence_state = "closed"
                    _fence_buffer = ""
                    if out_parts and not out_parts[-1].endswith("\n\n"):
                        if out_parts[-1].endswith("\n"):
                            out_parts[-1] += "\n"
                        else:
                            out_parts.append("\n\n")
                i += 1
                continue
            if ch == "`":
                j = i
                while j < n and j - i < 3 and delta[j] == "`":
                    j += 1
                run_len = j - i
                if run_len == 3:
                    _fence_state = "open"
                    _fence_buffer = ""
                    if out_parts and not out_parts[-1].endswith("\n\n") and not out_parts[-1].endswith("\n"):
                        out_parts.append("\n\n")
                    i = j
                    continue
                out_parts.append(delta[i:j])
                i = j
                continue
            out_parts.append(ch)
            i += 1
        visible_delta = "".join(out_parts)
        if "\n" in visible_delta:
            parts = visible_delta.split("\n")
            new_parts: list[str] = []
            for idx, p in enumerate(parts):
                new_parts.append(p)
                if idx < len(parts) - 1 and p != "":
                    new_parts.append("")
            visible_delta = "\n".join(new_parts)
        full_content += visible_delta
        return visible_delta

    # ── 事件日志（Redis Stream）：所有对外事件按序落盘，供断线/刷新后按游标回放 ──
    # 为什么用"缓冲 + 单写入者"而不是每个 _publish 直接 await：
    #   _publish 是同步函数、调用点有 18 处；若各自 create_task 去 XADD，
    #   多个网络往返会交错，日志顺序可能与事件产生顺序不一致（回放就会错乱）。
    #   单写入者按 FIFO 顺序写，天然保序。
    _log_buffer: list[dict] = []
    _log_stop = asyncio.Event()

    async def _log_writer() -> None:
        while True:
            if _log_buffer:
                await event_stream.publish(session_id, _log_buffer.pop(0))
                continue
            if _log_stop.is_set():
                return
            try:
                await asyncio.wait_for(_log_stop.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass

    _log_task = asyncio.create_task(
        _log_writer(), name=f"chat-eventlog-{session_id}"
    ) if session_id else None

    def _publish(ev: dict) -> None:
        if session_id:
            _log_buffer.append(ev)   # 事件日志（保序缓冲）
            # 注意：f"{session_id}" 转为注册表 key（str），否则 UUID 对象查不到任务
            chat_registry.publish(str(session_id), ev)

    def _update_text_snapshot() -> None:
        """更新注册表内存快照 + 节流写 Redis（供断线重连补发）。"""
        nonlocal last_redis_write
        if session_id:
            chat_registry.update_text(str(session_id), full_content)
            now = time.monotonic()
            if now - last_redis_write >= STREAM_TEXT_SNAPSHOT_INTERVAL:
                last_redis_write = now
                _write_streamed_text(session_id, full_content)

    async with async_session_factory() as db:
        try:
            async for event in _idle_guard(event_factory(db), label="chat_task_stream"):
                ev_type = event["type"]
                if ev_type == "text":
                    raw_delta = event["content"]
                    visible_delta = _strip_and_normalize(raw_delta)
                    visible_delta = _filter_status_text(visible_delta)
                    visible_delta = _filter_canvas_tool_result_lines(visible_delta)
                    if visible_delta.strip():
                        _update_text_snapshot()
                        _publish({"type": "message", "delta": visible_delta})
                elif ev_type == "tool_call":
                    _publish({"type": "tool_call", "name": event["name"], "args": event.get("args", {})})
                elif ev_type == "tool_result":
                    tname = event.get("name") or ""
                    result_str = event.get("result", "")
                    result_narration = ""
                    try:
                        parsed = json.loads(result_str) if isinstance(result_str, str) else {}
                        if tname == "query_sql":
                            if parsed.get("error"):
                                err_msg = str(parsed.get("error"))[:80]
                                result_narration = f"> 查询失败：{err_msg}\n"
                        elif tname == "render_chart":
                            if parsed.get("error"):
                                result_narration = f"> 图表生成失败：{parsed.get('error')}\n"
                    except Exception:
                        pass
                    if result_narration:
                        narrated = _strip_and_normalize("\n\n" + result_narration)
                        if narrated:
                            _update_text_snapshot()
                            _publish({"type": "message", "delta": narrated})
                    _publish({"type": "tool_result", "name": event["name"], "result": event["result"]})
                elif ev_type == "plan":
                    steps = (event.get("plan") or {}).get("steps", [])
                    _publish({
                        "type": "plan", "mode": "orchestrator",
                        "steps": [s.get("tool") for s in steps if isinstance(s, dict)],
                    })
                elif ev_type == "status":
                    _publish({
                        "type": "status",
                        "message": event.get("message", ""),
                        "phase": event.get("phase"),
                        "degradation": event.get("degradation"),
                    })
                elif ev_type == "chart":
                    chart_type = event.get("chart_type")
                    chart_option = event.get("option")
                    if chart_option is None:
                        continue
                    chart_obj = {"chart_type": chart_type, "option": chart_option}
                    key = _chart_key(chart_obj)
                    if key in seen_chart_keys:
                        continue
                    seen_chart_keys.add(key)
                    collected_charts.append(chart_obj)
                elif ev_type == "intent":
                    _publish({
                        "type": "intent",
                        "intent": event.get("intent"), "confidence": event.get("confidence"),
                        "needs_plan": event.get("needs_plan"), "degraded": event.get("degraded"),
                    })
                elif ev_type == "decision":
                    _publish({
                        "type": "decision",
                        "round": event.get("round"), "action": event.get("action"),
                        "tool": event.get("tool"), "reason": event.get("reason"),
                        "degraded": event.get("degraded"),
                    })
                elif ev_type == "progress":
                    _publish({
                        "type": "progress",
                        "round": event.get("round"), "index": event.get("index"),
                        "total": event.get("total"), "title": event.get("title"),
                        "status": event.get("status"), "note": event.get("note"),
                        "tool": event.get("tool"),
                    })
                elif ev_type == "report":
                    if not full_content.strip():
                        visible = _filter_status_text(_strip_and_normalize(event.get("content", "")))
                        if visible.strip():
                            _update_text_snapshot()
                            _publish({"type": "message", "delta": visible})
                elif ev_type == "memory_saved":
                    _publish({"type": "memory_saved", "chars": event.get("chars", 0)})
                elif ev_type == "done":
                    _publish({"type": "done", "charts": collected_charts, "degraded": event.get("degraded", False)})
                elif ev_type == "error":
                    _publish({"type": "error", "message": event["message"]})
                elif ev_type == "compressed_history":
                    await _save_memory(db, session_id, event)
        except AINotConfiguredError:
            _publish({"type": "error", "message": "AI 未配置"})
        except AIUpstreamError as e:
            _publish({"type": "error", "message": str(e)})
        except Exception:
            _log.exception("Agent chat task error")
            _publish({"type": "error", "message": "服务异常，请查看日志"})
        else:
            # 只有正常跑完事件流（含 Agent 内部 error 事件）才算完成
            completed = True
        finally:
            # 收尾（任何退出路径都执行）：**冲刷事件日志** → 落库占位行 → 文本快照 → 任务态 → 释放锁
            # 事件日志必须最先冲刷：续收方靠日志里的 done 判断"这一轮结束了"，
            # 若 done 还没落盘就退出，重连者会一直阻塞等新事件。
            if _log_task is not None:
                _log_stop.set()
                try:
                    await asyncio.wait_for(_log_task, timeout=3.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    _log_task.cancel()
                except Exception:  # noqa: BLE001
                    _log.debug("chat event log flush failed", exc_info=True)
            if assistant_msg_id is not None:
                try:
                    row = await db.get(AIMessage, assistant_msg_id)
                    if row is not None:
                        row.content = full_content
                        row.chart_data = {"charts": collected_charts} if collected_charts else None
                        await db.commit()
                except Exception:
                    await db.rollback()
                    _log.exception("chat_task_save_failed")
            if session_id:
                _write_streamed_text(session_id, full_content)
            if task_key:
                try:
                    repo = get_cache_repository()
                    if completed:
                        repo.delete(task_key)  # 正常完成：清除 running 标记
                    elif not repo.exists(task_key):
                        repo.set(
                            task_key,
                            json.dumps({
                                "status": "interrupted",
                                "chars": len(full_content),
                                "interrupted_at": datetime.now(timezone.utc).isoformat(),
                            }),
                            ttl=settings.redis_ttl,
                        )
                except Exception:
                    pass
            # 会话锁：任务结束才释放（连接断开不释放，让任务继续独占到跑完）
            release_session_lock(session_id, lock_token)


