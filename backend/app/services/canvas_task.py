# -*- coding: utf-8 -*-
"""画布后台任务内核（从 API 模块抽出，供 API 进程与 worker 进程共用）。

为什么独立成模块
----------------
画布是主入口，但它的任务体（Agent 执行 + 事件翻译 + 落块计数 + 收尾）整体写在
API 请求闭包里，只有同一个进程能跑——这就是"对话在 worker、画布在进程内"的
不对称根源。抽出后 worker 进程也能 import 到这份内核（与 chat_task 同一前置条件）。

与 run_chat_task 的分工差异（**别共用一个函数**）：
  · 对话入口消费的是 Orchestrator/ReAct 的旧事件协议（text→过滤状态行、chart 聚合…）；
  · 画布入口消费的是 Lead 单 Agent 事件协议（tool_result 内嵌 canvas_action、
    done 抑制到流尾、兜底文案按 canvas_actions_count）——两套翻译规则本就不同，
    合并只会得到两个 if 大杂烩。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from app.config import settings
from app.core.cache import get_cache_repository
from app.core.database import async_session_factory
from app.models.ai_message import AIMessage
from app.services.chat_stream_registry import chat_registry
from app.services.chat_task import _idle_guard
from app.services.event_stream import event_stream
from app.services.llm_client import AINotConfiguredError, AIUpstreamError
from app.services.session_lock import release_session_lock

logger = logging.getLogger("lvco.services.canvas_task")
_log = logger


async def run_canvas_task(
    *,
    session_id: str | None,
    lock_token: str | None,
    assistant_msg_id: Any | None,
    task_key: str | None,
    event_factory: Any,
) -> None:
    """画布后台任务主体：消费 Agent 事件流 → 翻译 → 事件日志 + 注册表广播 → 收尾。

    事件日志必须**先于**注册表广播写（顺序刻意）：
      广播是实时的、日志是可回放的。先广播后写日志，断线重连的读者可能恰好在
      "已广播、未落日志"的缝隙里读到旧游标，那条事件就永久丢了。
      先落日志最坏只是重连时多回放一条，不会丢。
    """
    full_content = ""
    canvas_actions_count = 0
    stream_degraded = False  # 事件流里出现过的降级标记，随流尾唯一的 done 下发
    completed = False        # 正常收尾（走到 emit done）才置 True；否则视为中断

    async with async_session_factory() as db:
        # ── 事件日志（Redis Stream）：直接顺序 await（画布事件粒度大，无需缓冲器）──
        async def _publish(ev: dict) -> None:
            if session_id:
                try:
                    await event_stream.publish(session_id, ev)
                except Exception:  # noqa: BLE001
                    _log.debug("canvas event publish failed", exc_info=True)
                # 注册表只在本进程有订阅者时才有意义；队列模式下它是空操作，无害
                chat_registry.publish(str(session_id), ev)

        def _update_text(acc: str) -> None:
            if session_id:
                chat_registry.update_text(str(session_id), acc)

        try:
            async for event in _idle_guard(event_factory(db), label="canvas_task_stream"):
                ev_type = event.get("type")
                if ev_type == "text":
                    delta = event.get("content", "")
                    full_content += delta
                    _update_text(full_content)
                    await _publish({"type": "message", "delta": delta})
                elif ev_type == "plan":
                    # entry=canvas 与对话入口（entry=chat, mode=orchestrator）区分，
                    # 观测方可从 SSE 直接识别规划来源
                    steps = (event.get("plan") or {}).get("steps", [])
                    await _publish({
                        "type": "plan", "entry": "canvas", "mode": "canvas_orchestrator",
                        "steps": [{"tool": s.get("tool"), "goal": s.get("goal")}
                                  for s in steps if isinstance(s, dict)],
                    })
                elif ev_type == "status":
                    await _publish({
                        "type": "status", "message": event.get("message", ""),
                        "phase": event.get("phase"),
                        "degradation": event.get("degradation"),
                    })
                elif ev_type == "tool_call":
                    await _publish({"type": "tool_call", "name": event.get("name", ""),
                                    "args": event.get("args", {}) or {}})
                elif ev_type == "tool_result":
                    result_raw = event.get("result", "")
                    await _publish({"type": "tool_result", "name": event.get("name", ""),
                                    "result": result_raw})
                    # 画布工具返回内嵌 canvas_action：提取后转发为独立 SSE 事件，
                    # 前端据此实时落块。
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else {}
                        action = r.get("canvas_action") if isinstance(r, dict) else None
                        if isinstance(action, dict):
                            canvas_actions_count += 1
                            await _publish({"type": "canvas_action", **action})
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif ev_type == "intent":
                    # source=model（模型自述）/ derived（代码推断）必须透传
                    await _publish({
                        "type": "intent",
                        "intent": event.get("intent"), "confidence": event.get("confidence"),
                        "needs_plan": event.get("needs_plan"), "degraded": event.get("degraded"),
                        "source": event.get("source"), "step": event.get("step"),
                    })
                elif ev_type == "decision":
                    await _publish({
                        "type": "decision", "round": event.get("round"),
                        "action": event.get("action"), "tool": event.get("tool"),
                        "reason": event.get("reason"), "degraded": event.get("degraded"),
                    })
                elif ev_type == "confirm_request":
                    # HITL 确认卡片（§6 不变量 7）：goal=挂起目标，question=卡片文案
                    await _publish({"type": "confirm_request", "goal": event.get("goal"),
                                    "question": event.get("question")})
                elif ev_type == "progress":
                    await _publish({
                        "type": "progress", "round": event.get("round"),
                        "index": event.get("index"), "total": event.get("total"),
                        "title": event.get("title"), "status": event.get("status"),
                        "note": event.get("note"), "tool": event.get("tool"),
                    })
                elif ev_type == "report":
                    if not full_content.strip():
                        report_delta = event.get("content", "")
                        if report_delta:
                            full_content += report_delta
                            _update_text(full_content)
                            await _publish({"type": "message", "delta": report_delta})
                elif ev_type == "memory_saved":
                    await _publish({"type": "memory_saved", "chars": event.get("chars", 0)})
                elif ev_type == "error":
                    await _publish({"type": "error", "message": event.get("message", "服务异常")})
                elif ev_type == "done":
                    # 不下发：done 统一在流尾补发一次。原因：事件流里可能混入子执行器的
                    # 内部 done，提前下发会让前端在子任务落块完就标记完成（一次请求两个 done）。
                    stream_degraded = bool(event.get("degraded", stream_degraded))
                elif ev_type == "compressed_history":
                    # 压缩记忆回流：持久化到会话级记忆（与对话入口同一份实现）
                    from app.services.chat_task import _save_memory

                    await _save_memory(db, session_id, event)

            # ---- 收尾：保存助手消息 + 清任务态 + 下发唯一 done ----
            # 即使 LLM 全程只调用工具（full_content 空），只要有实际画布动作，
            # 也生成一份兜底文案，保证下次加载历史时链路完整。
            save_content = full_content.strip()
            if not save_content and canvas_actions_count > 0:
                save_content = (
                    f"本次分析通过画布工具完成：已在画布执行 {canvas_actions_count} 次落块操作，"
                    "请查看画布内容与工作台执行记录。"
                )
            assistant_row = await db.get(AIMessage, assistant_msg_id) if assistant_msg_id else None
            if assistant_row is not None:
                assistant_row.content = save_content
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.rollback()
                # 任务正常结束：清除 running 标记，避免误报"未完成"
                try:
                    get_cache_repository().delete_shared(f"ai:canvas:task:{session_id}")
                except Exception:  # noqa: BLE001
                    pass

            completed = True
            await _publish({"type": "done", "degraded": stream_degraded})

        except AINotConfiguredError:
            await _publish({"type": "error", "message": "AI 未配置"})
            await _publish({"type": "done", "degraded": True})
        except AIUpstreamError as e:
            await _publish({"type": "error", "message": str(e)})
            await _publish({"type": "done", "degraded": True})
        except Exception:  # noqa: BLE001
            _log.exception("Canvas task error")
            await _publish({"type": "error", "message": "服务异常: 请查看日志"})
            await _publish({"type": "done", "degraded": True})
        finally:
            # 兜底：异常/中断时，把已产出的文本补存进占位行，任务态标记 interrupted。
            am = await db.get(AIMessage, assistant_msg_id) if assistant_msg_id else None
            if am is not None:
                try:
                    if not am.content and full_content.strip():
                        am.content = full_content.strip()
                    # 中断可见：超时/上游断流/被取消时，会话里留一条明确提示
                    if not completed:
                        hint = (
                            "\n\n> ⚠️ 本轮生成已中断（上游超时或连接断开），已保留前面生成的内容。"
                            "可以再发一句「继续」，我会接着做。"
                        )
                        if hint.strip() not in (am.content or ""):
                            am.content = (am.content or "") + hint
                    await db.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                if task_key:
                    try:
                        repo = get_cache_repository()
                        if repo.exists(task_key):
                            repo.set_shared(
                                task_key,
                                json.dumps({
                                    "status": "interrupted",
                                    "chars": len(full_content),
                                    "interrupted_at": datetime.now(timezone.utc).isoformat(),
                                }),
                                ttl=settings.redis_ttl,
                            )
                    except Exception:  # noqa: BLE001
                        pass
            # 会话锁：正常结束/异常都走这里释放（TTL 仅作进程崩溃的兜底）
            release_session_lock(session_id, lock_token)
