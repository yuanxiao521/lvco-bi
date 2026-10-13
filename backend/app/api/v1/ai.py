import asyncio
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_cache_repository, get_current_user
from app.config import settings
from app.core.database import async_session_factory, get_db
from app.core.duckdb_client import duckdb_client
from app.core.limiter import limiter
from app.models.ai_message import AIMessage, AIMessageRole
from app.models.ai_session import AISession
from app.models.canvas import Canvas
from app.models.datasource import DataSource
from app.models.user import User
from app.schemas import (
    AICleanRequest,
    AIMessageCreate,
    AIMessageResponse,
    AIQueryRequest,
    AIRecommendRequest,
    AIRecommendResult,
    AISessionCreate,
    AISessionDetail,
    AISessionResponse,
    CanvasChatRequest,
    DataChatRequest,
    InsightsRequest,
    PolishRequest,
    SuccessResponse,
)
from app.services.ai_service import AIService, VALID_CHART_TYPES
from app.services.ai_prompts import CANVAS_SYSTEM
from app.services.canvas_tools import CANVAS_TOOL_NAMES
from app.services.session_lock import acquire_session_lock, release_session_lock
from app.services.chat_stream_registry import chat_registry
from app.services.event_stream import event_stream

# 对话后台任务内核已抽到 services/chat_task.py（API 与 worker 两进程共用同一份执行逻辑）：
# 队列消费端在另一个进程，必须能 import 到它。此处 re-export 保持旧名可用，调用点零改动。
from app.services.chat_task import (
    CHAT_STREAM_TEXT_PREFIX,
    STREAM_TEXT_SNAPSHOT_INTERVAL,
    _chat_text_key,
    _idle_guard,
    _save_memory,
    _write_streamed_text,
    run_chat_task as _run_chat_task,
)

# 对话入口后台续跑注册表（进程内单例）+ Redis 文本快照 key 前缀。
# Agent 任务与 SSE 连接解耦：断开只退订、不杀任务，刷新后带同一 session_id 重连续收。
#
# 续收有两层（见 services/event_stream.py 的原理说明）：
#   1) **事件日志（Redis Stream，主）**：所有对外事件按序 XADD，前端带游标重连即回放，
#      canvas_action/decision/progress 等刷新不再丢失；
#   2) **文本快照（旧路径，兜底）**：单个字符串，Stream 不可用时仍能补发可见文本。
# （chat_registry 单例与两个常量已随内核抽到 services 层，见上方 import）

# 画布助手允许的工具 = 画布专属落块工具 + 基础查数/出图工具（先查再落）。
# 普通润色、清洗建议等与画布无关的工具不会出现在画布助手里，避免 LLM 调错（如调 render_chart 只出 option 不落块）。
_CANVAS_QUERY_TOOL_NAMES = frozenset({
    "list_datasources", "list_fields", "list_metrics", "query_sql", "query_engine",
})
CANVAS_ALLOWED_TOOL_NAMES = frozenset(CANVAS_TOOL_NAMES | _CANVAS_QUERY_TOOL_NAMES)
from app.services.context_utils import align_history_pairs as _align_history_pairs
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient

# 会话级并发锁被占用时的 SSE error 文案（对话/画布两个入口共用）
_SESSION_BUSY_MSG = "上一轮还在处理中，请等它结束后再发。"

router = APIRouter(prefix="/ai", tags=["AI助手"])


async def _load_session_memory(
    db: AsyncSession, session_id
) -> tuple[str | None, int, str | None, int]:
    """读取会话级记忆：(长期摘要, 已并入轮数, 合并水位, 连续失败次数)。

    无会话 / 无记忆行 / 读取失败一律返回空值，调用方按"无长期记忆"处理。
    空字符串摘要也归一成 None（首次合并即失败时会建一行空记忆承载熔断计数）。
    """
    if not session_id:
        return None, 0, None, 0
    try:
        from app.repositories.ai_session_repository import SQLAlchemyAIMemoryRepository
        memory = await SQLAlchemyAIMemoryRepository(db).get_by_session(session_id)
        if memory is None:
            return None, 0, None, 0
        wm = getattr(memory, "last_merged_message_id", None)
        return (
            (memory.summary or "") or None,
            int(memory.covered_rounds or 0),
            str(wm) if wm else None,
            int(getattr(memory, "merge_fail_count", 0) or 0),
        )
    except Exception:
        _log.warning("load_ai_memory_failed", exc_info=True)
        return None, 0, None, 0


def _filter_canvas_history(rows, current_msg_id=None) -> list[dict]:
    """画布历史过滤：只跳过"空占位"的 assistant 行（保留有内容的助手消息，含反问/说明），
    按 id 排除本条用户消息（它由 Lead 统一 add_turn，避免窗口里重复），
    最后把窗口对齐到"问答成对"（丢掉裁剪产生的孤立回答开场）。"""
    history: list[dict] = []
    for pm in rows:
        if current_msg_id is not None and getattr(pm, "id", None) == current_msg_id:
            continue
        if pm.role == AIMessageRole.assistant and not (pm.content or "").strip():
            continue
        history.append({"role": pm.role.value, "content": pm.content})
    return _align_history_pairs(history)


async def _load_server_window(
    db: AsyncSession,
    session_id: uuid.UUID | None,
    *,
    exclude_msg_id: Any | None = None,
    max_rounds: int | None = None,
) -> list[dict]:
    """服务端窗口（阶段 3）：按会话从 DB 取最近 N 轮——窗口唯一所有者。

    为什么收归服务端：旧实现聊天入口用前端 slice(-10)、画布入口 LIMIT 13，
    窗口大小由客户端决定（换客户端/清缓存即失忆），服务端的 30 条上限形同虚设。

    规则（与 _filter_canvas_history 一致）：
    - 排除本条用户消息（由 Lead 统一 add_turn，避免窗口重复）；
    - 跳过空占位 assistant 行；
    - 成对对齐（丢掉裁剪产生的孤立回答开场）。
    """
    if not session_id:
        return []
    from app.config import settings as _settings

    rounds = max(1, int(max_rounds or getattr(_settings, "WINDOW_MAX_ROUNDS", 8) or 8))
    # 每轮 2 条（user+assistant），多取 1 条给成对对齐做余量
    limit = rounds * 2 + 1
    prior = (
        await db.execute(
            select(AIMessage)
            .where(AIMessage.session_id == session_id)
            # 次级键 role 与 created_at 同向（见画布入口注释）；多取 2 条余量
            .order_by(AIMessage.created_at.desc(), AIMessage.role.desc())
            .limit(limit + 1)
        )
    ).scalars().all()
    prior = list(reversed(prior))
    history: list[dict] = []
    for pm in prior:
        if exclude_msg_id is not None and getattr(pm, "id", None) == exclude_msg_id:
            continue
        if pm.role == AIMessageRole.assistant and not (pm.content or "").strip():
            continue
        history.append({"role": pm.role.value, "content": pm.content})
    return _align_history_pairs(history)[: rounds * 2]


def _build_chart_summary_note(prior_msgs, budget: int) -> str:
    """把"最近几轮已生成的图表"渲染成一段注入文本，受总字符预算约束。

    为什么需要预算：旧实现按"每图 600 字符 × 最多 6 条消息"累加且**无总量上限**，
    一个多图答案就能灌进上万字符。这里是"免费可省的一层"——先裁状态载荷，再谈摘要。
    """
    parts: list[str] = []
    used = 0
    for pm in prior_msgs or []:
        if getattr(pm, "role", None) != AIMessageRole.assistant:
            continue
        chart_data = getattr(pm, "chart_data", None)
        charts = chart_data.get("charts") if isinstance(chart_data, dict) else None
        if not charts:
            continue
        for c in charts:
            line = (
                f"- {c.get('chart_type', '?')} 图表："
                f"{json.dumps(c.get('option', {}), ensure_ascii=False)[:600]}"
            )
            if used + len(line) > budget:
                break
            parts.append(line)
            used += len(line) + 1
        if used >= budget:
            break
    if not parts:
        return ""
    return "【上一轮已生成的图表，请勿重复生成同类型图表】\n" + "\n".join(parts)


# _lead_stream_guard 已移到 services/chat_task.py（执行内核的一部分）：
# 队列模式下 agent 异常必须同样被转成 done 事件，否则事件日志没有终止信号、前端会一直转圈。
from app.services.chat_task import lead_stream_guard as _lead_stream_guard  # noqa: E402


# 事件流空闲看门狗阈值（秒）：超过这么久没有任何事件，判定 Agent 内部停住
# （典型：上游 LLM 一直发心跳但不出 token，httpx 的 read timeout 只兜"字节间隔"兜不住它）
STREAM_IDLE_TIMEOUT = 90.0


def _read_streamed_text(session_id: object) -> str:
    """读取后台任务已产出的文本快照（Redis 兜底；正常应命中注册表内存 full_text）。"""
    try:
        return str(get_cache_repository().get(_chat_text_key(session_id)) or "")
    except Exception:
        return ""


async def _yield_streamed_text_snapshot(session_id: object):
    """（订阅侧）先补发一段 message delta 携带当前已产出全量文本，再开始收增量。"""
    text = ""
    rt = chat_registry.get(str(session_id)) if session_id else None
    if rt is not None and rt.full_text:
        text = rt.full_text
    else:
        text = _read_streamed_text(session_id)
    if text and text.strip():
        yield f"data: {json.dumps({'type': 'message', 'delta': text}, ensure_ascii=False)}\n\n"


async def _yield_subscription_events(session_id: object):
    """（订阅侧）把注册表广播的事件转成 SSE 行；任务结束（哨兵）自然退出。"""
    async for ev in chat_registry.subscribe(str(session_id)):
        if isinstance(ev, dict):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"


def _sse_line(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


async def _stream_from_event_log(
    task_sid: object,
    *,
    max_seconds: float = 1800.0,
    idle_stop=None,
    max_idle_seconds: float | None = None,
):
    """（订阅侧）纯事件日志读取：回放历史 + 阻塞续读，直到 done 或超时。

    队列模式（TASK_QUEUE_ENABLED）与断线续收共用这套读取逻辑——两者的共同点是
    **执行者不在本进程**：本进程只能从事件日志按游标读，无法依赖进程内注册表广播。
    """
    cursor = "0"
    saw_done = False
    text_buf: list[str] = []

    for eid, ev in await event_stream.read_after(task_sid, "0"):
        cursor = eid
        et = ev.get("type")
        if et == "canvas_action":
            # 非幂等副作用：回放会重复落块。画布真值已在 DB，前端重载即可。
            continue
        if et == "message":
            text_buf.append(str(ev.get("delta") or ""))
            continue
        if text_buf:
            yield _sse_line({"type": "message", "delta": "".join(text_buf)})
            text_buf = []
        if et == "done":
            saw_done = True
        yield _sse_line(ev)
    if text_buf:
        yield _sse_line({"type": "message", "delta": "".join(text_buf)})
    if not saw_done:
        deadline = time.monotonic() + max_seconds
        # 「无进展」计时：最后一条事件的时刻。worker 中途被杀时日志不会再有新事件，
        # 若不设上限，读者会一直阻塞到 max_seconds（30 分钟）——用户干等。
        last_activity = time.monotonic()

        def _stop() -> bool:
            if idle_stop is not None and idle_stop():
                return True
            if max_idle_seconds is not None and (time.monotonic() - last_activity) > max_idle_seconds:
                return True
            return time.monotonic() > deadline

        async for _eid, ev in event_stream.tail(task_sid, cursor, idle_stop=_stop):
            last_activity = time.monotonic()
            yield _sse_line(ev)
            if ev.get("type") == "done":
                saw_done = True
                break

    if not saw_done:
        # 兜底终止信号：日志里没有 done（任务硬崩溃/超时被掐），也要让前端解锁输入框，
        # 否则前端会一直停在"思考中"。
        yield _sse_line({"type": "done"})


# ── 对话后台续跑：任务主体 ───────────────────────────────────────
@router.get("/sessions")
async def list_sessions(
    entry: str | None = Query(None, description="会话入口：chat / canvas，不传返回全部"),
    canvas_id: UUID | None = Query(None, description="归属画布（配合 entry='canvas' 使用）"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    conditions = [AISession.user_id == current_user.id]
    if entry is not None:
        conditions.append(AISession.entry == entry)
    if canvas_id is not None:
        conditions.append(AISession.entry == "canvas")
        conditions.append(AISession.canvas_id == canvas_id)
    result = await db.execute(
        select(AISession)
        .where(*conditions)
        .order_by(AISession.created_at.desc())
    )
    items = list(result.scalars().all())
    cache_repo = get_cache_repository()
    data = []
    for s in items:
        d = AISessionResponse.model_validate(s).model_dump(mode="json", by_alias=True)
        # 进程内后台任务是否在跑：前端用它判断"刷新后自动续收"（只对对话入口有意义）
        try:
            d["running"] = bool(chat_registry.is_running(str(s.id))) if entry == "chat" else False
        except Exception:
            d["running"] = False
        # Redis 任务态标记：供前端提示「上次对话未完成」（正常完成时 key 已被删除）
        try:
            task_json = cache_repo.get(f"ai:chat:task:{s.id}")
            d["interrupted"] = bool(
                task_json and json.loads(task_json).get("status") == "interrupted"
            )
        except Exception:
            d["interrupted"] = False
        data.append(d)
    return SuccessResponse(data=data)


@router.get("/canvases/{canvas_id}/sessions")
async def list_canvas_sessions(
    canvas_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """画布内会话列表（entry='canvas'），按创建时间倒序，供画布助手"历史列表"使用。"""
    canvas = (
        await db.execute(
            select(Canvas).where(Canvas.id == canvas_id, Canvas.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )
    result = await db.execute(
        select(AISession)
        .where(
            AISession.user_id == current_user.id,
            AISession.entry == "canvas",
            AISession.canvas_id == canvas_id,
        )
        .order_by(AISession.created_at.desc())
    )
    items = list(result.scalars().all())
    cache_repo = get_cache_repository()
    data = []
    for s in items:
        d = AISessionResponse.model_validate(s).model_dump(mode="json", by_alias=True)
        # Redis 任务态标记：供前端提示「上次对话未完成」（正常完成时 key 已被删除）
        try:
            task_json = cache_repo.get(f"ai:canvas:task:{s.id}")
            d["interrupted"] = bool(
                task_json and json.loads(task_json).get("status") == "interrupted"
            )
        except Exception:
            d["interrupted"] = False
        data.append(d)
    return SuccessResponse(data=data)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: AISessionCreate | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    payload = body or AISessionCreate()
    session = AISession(
        user_id=current_user.id,
        entry="chat",
        title=payload.title,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return SuccessResponse(
        data=AISessionResponse.model_validate(session).model_dump(mode="json", by_alias=True)
    )


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: UUID,
    body: AISessionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """重命名会话标题"""
    result = await db.execute(
        select(AISession).where(
            AISession.id == session_id, AISession.user_id == current_user.id
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "会话不存在"},
        )
    if body.title is not None:
        session.title = body.title
        await db.flush()
        await db.refresh(session)
    return SuccessResponse(
        data=AISessionResponse.model_validate(session).model_dump(mode="json", by_alias=True)
    )


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    sess = await db.execute(
        select(AISession).where(
            AISession.id == session_id, AISession.user_id == current_user.id
        )
    )
    if sess.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "会话不存在"},
        )
    result = await db.execute(
        select(AIMessage)
        .where(AIMessage.session_id == session_id)
        # 次级键 role 不可省：同一轮的 user 提问与 assistant 占位在同一事务提交，
        # created_at 取自 now()（事务开始时间）→ 两行时间戳完全相同，而 id 是随机 uuid
        # 做不了次级键。缺它则结果依赖不稳定排序，问答对会颠倒。
        # 枚举标签序为 {user,assistant}，故 asc 即"提问在前、回答在后"。
        .order_by(AIMessage.created_at.asc(), AIMessage.role.asc())
    )
    items = list(result.scalars().all())
    return SuccessResponse(
        data=[AIMessageResponse.model_validate(m).model_dump(mode="json", by_alias=True) for m in items]
    )


@router.post("/sessions/{session_id}/messages")
@limiter.limit("30/minute")
async def send_message(
    request: Request,
    session_id: UUID,
    body: AIMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Validate session exists and belongs to current_user
    session = (
        await db.execute(
            select(AISession).where(
                AISession.id == session_id, AISession.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "会话不存在"},
        )

    # Pre-check: AI not configured → 503
    if not settings.is_ai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "AI_NOT_CONFIGURED",
                "message": "请在 .env 配置 OPENAI_API_KEY",
            },
        )

    # Save user message
    user_msg = AIMessage(
        session_id=session_id,
        role=AIMessageRole.user,
        content=body.content,
    )
    db.add(user_msg)
    await db.flush()

    # Auto-title: use first user message (truncated) if session has default title
    if session.title in (None, "新对话"):
        title = body.content[:30] + ("..." if len(body.content) > 30 else "")
        session.title = title
        db.add(session)
        await db.flush()

    # Load history (all messages for this session ordered by created_at ASC)
    result = await db.execute(
        select(AIMessage)
        .where(AIMessage.session_id == session_id)
        # 次级键 role 不可省：同一轮的 user 提问与 assistant 占位在同一事务提交，
        # created_at 取自 now()（事务开始时间）→ 两行时间戳完全相同，而 id 是随机 uuid
        # 做不了次级键。缺它则结果依赖不稳定排序，问答对会颠倒。
        # 枚举标签序为 {user,assistant}，故 asc 即"提问在前、回答在后"。
        .order_by(AIMessage.created_at.asc(), AIMessage.role.asc())
    )
    messages = list(result.scalars().all())
    history_list: list[dict[str, str]] = [
        {"role": m.role.value, "content": m.content} for m in messages
    ]

    async def event_generator():
        full_content = ""
        chart_data = None
        try:
            llm_client = LLMClient(settings)
            ai_service = AIService(llm_client)

            # Stream tokens from AI service
            async for token in ai_service.chat_stream(history_list, body.content):
                full_content += token
                event = json.dumps({"type": "message", "delta": token}, ensure_ascii=False)
                yield f"data: {event}\n\n"

            # Parse chart from content if ```json block found
            if "```json" in full_content:
                try:
                    import re

                    match = re.search(
                        r"```json\s*\n(.*?)\n```", full_content, re.DOTALL
                    )
                    if match:
                        chart_data = json.loads(match.group(1))
                        yield f"data: {json.dumps({'type': 'chart', 'payload': chart_data}, ensure_ascii=False)}\n\n"
                except Exception:
                    pass

            # Save assistant message to DB
            assistant_msg = AIMessage(
                session_id=session_id,
                role=AIMessageRole.assistant,
                content=full_content,
                chart_data=chart_data,
            )
            db.add(assistant_msg)
            await db.commit()

            # Send done event
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

        except AINotConfiguredError:
            yield f"data: {json.dumps({'type': 'error', 'message': '请在 .env 配置 OPENAI_API_KEY'}, ensure_ascii=False)}\n\n"
        except AIUpstreamError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'AI 服务异常: {str(e)}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(AISession)
        .where(AISession.id == session_id, AISession.user_id == current_user.id)
        .options(selectinload(AISession.messages))
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "会话不存在"},
        )
    return SuccessResponse(
        data=AISessionDetail.model_validate(session).model_dump(mode="json", by_alias=True)
    )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    sess = await db.execute(
        select(AISession).where(
            AISession.id == session_id, AISession.user_id == current_user.id
        )
    )
    session = sess.scalar_one_or_none()
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "会话不存在"},
        )
    await db.delete(session)
    await db.flush()
    return SuccessResponse(data={"message": "已删除"})


@router.post("/chat/stream")
async def data_chat_stream(
    request: Request,
    body: DataChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Data-aware chat: Agent 模式 — 自动发现数据源、执行查询、生成图表。

    所有查询经三层安全防护（输入安全 → 意图分析 → SQL 输出控制）。
    当 datasource_id 提供时，Agent 直接使用该数据源；
    未提供时，Agent 自动调用 list_datasources 工具浏览可用数据。
    """
    if not settings.is_ai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_NOT_CONFIGURED", "message": "请配置 OPENAI_API_KEY"},
        )

    async def event_generator():
        # Resolve session if session_id provided
        session = None
        if body.session_id:
            try:
                sid_uuid = UUID(body.session_id)
            except (ValueError, TypeError):
                sid_uuid = None
            if sid_uuid:
                session = (
                    await db.execute(
                        select(AISession).where(
                            AISession.id == sid_uuid, AISession.user_id == current_user.id
                        )
                    )
                ).scalar_one_or_none()
                # 加固：对话入口不允许续聊画布会话，防止误串记忆
                if session is not None and session.entry == "canvas":
                    session = None

        # Create session if not exists
        if session is None and body.message.strip():
            session = AISession(
                user_id=current_user.id,
                entry="chat",
                title="新对话",
            )
            db.add(session)
            await db.flush()
            await db.refresh(session)

        session_id = session.id if session else None
        # 后台任务在注册表里的 key：无会话时用一次性 key（事件照常广播，只是无法续收）
        task_sid = str(session_id) if session_id else f"anon-{uuid.uuid4().hex}"

        # ── 续收分支：刷新/断线后带同一 session_id 重连 → 命中运行中的任务则纯订阅 ──
        # （不抢锁、不落新消息；任务结束在后端继续跑，本连接只负责补发快照 + 续收事件）
        if body.resume:
            # ── 续收（阶段 B：事件日志按游标回放）────────────────────────────────
            # 旧实现：先补发"文本快照"，再订阅**实时队列**——两步之间有缝隙，
            # 缝隙里产生的事件（工作台步骤/决策/落块）永远看不到，且只有文本能补发。
            # 新实现：事件已持久化为日志（Redis Stream），从游标 0 回放到最新，
            # 再从**同一游标**继续阻塞读增量——回放与实时无缝衔接，所有事件类型都在。
            # 判定"这一轮还在不在跑"必须**模式无关**（审查修复）：
            #   · 进程内模式：任务在 chat_registry 里，可以查它；
            #   · 队列模式：任务在 **worker 进程**里，本进程的注册表根本不知道它——
            #     用 is_running 判断会永远为 False，导致"只回 done、不回放不续读"（续收失效）。
            # 所以统一改为：**以事件日志为准**——日志里有 done 就是跑完了，没有就继续等。
            inprocess_running = bool(session_id) and chat_registry.is_running(str(session_id))

            if not inprocess_running and not await event_stream.has_events(task_sid):
                # 日志为空：两种可能
                #   a) 这一轮压根没执行过（或日志已过期）→ 直接收尾；
                #   b) 队列模式刚投递、worker 还没产出第一个事件（LLM 首包要几秒）
                #      → 给一个短窗口等首事件，避免刷新即"空答复"。
                from app.worker import worker_alive

                if not await worker_alive():
                    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                    return
                async for line in _stream_from_event_log(task_sid, max_seconds=60.0):
                    yield line
                return

            # 回放 + 续读：与队列模式共用同一段实现（_stream_from_event_log）。
            # idle_stop 只在进程内模式下有意义（注册表没了＝跑完了）；队列模式传 None，
            # 靠日志里的 done 或时间兜底退出。
            idle_stop = (
                (lambda: not chat_registry.is_running(str(session_id)))
                if inprocess_running
                else None
            )
            async for line in _stream_from_event_log(
                task_sid,
                idle_stop=idle_stop,
                # 队列模式（idle_stop 为 None）时用"无进展上限"兜底：
                # worker 被杀 → 日志不再更新 → 4 分钟后收尾，而不是干等到 30 分钟。
                max_idle_seconds=None if inprocess_running else 240.0,
            ):
                yield line
            return

        # ── 新消息分支：抢锁 → 落占位行 → 启动后台任务 → 本连接订阅 ──
        # 会话级并发锁：同一会话同一时刻只允许一轮在跑（连点两次会让 Agent 双跑：
        # 重复 LLM 花费 + 重复落块 + 历史里出现两条回复）。抢不到就直接拒，
        # 此时还没落任何消息，不会留下"没人回答"的僵尸消息。
        # 注意：锁的释放移交后台任务（任务结束才释放），连接断开不会释放锁。
        lock_token: str | None = None
        assistant_msg_id: Any | None = None
        task_key: str | None = None
        if session_id:
            lock_token = acquire_session_lock(session_id)
            if lock_token is None:
                yield f"data: {json.dumps({'type': 'error', 'message': _SESSION_BUSY_MSG}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return

            # Save user message + 占位 assistant 行（立即落库：断流/切页也不丢）
            user_msg_model = AIMessage(
                session_id=session_id,
                role=AIMessageRole.user,
                content=body.message,
            )
            db.add(user_msg_model)

            # Auto-title
            if session.title in (None, "新对话"):
                title = body.message[:30] + ("..." if len(body.message) > 30 else "")
                session.title = title
                db.add(session)

            # 占位 assistant 行：先落库标记"生成中"，后台任务结束后用真实内容更新
            assistant_msg = AIMessage(
                session_id=session_id,
                role=AIMessageRole.assistant,
                content="",
            )
            db.add(assistant_msg)

            # 会话 + 用户消息 + 占位行一次性提交，保证客户端断开后历史仍可读到
            await db.commit()
            assistant_msg_id = assistant_msg.id
            task_key = f"ai:chat:task:{session_id}"
            try:
                get_cache_repository().set(
                    task_key,
                    json.dumps({
                        "status": "running",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }),
                    ttl=settings.redis_ttl,
                )
            except Exception:
                _log.debug("chat task running cache set failed", exc_info=True)

            # Notify frontend about the new session
            yield f"data: {json.dumps({'type': 'session_created', 'session': AISessionResponse.model_validate(session).model_dump(mode='json', by_alias=True)}, ensure_ascii=False)}\n\n"

        # ── 构建 Agent 事件流生产闭包（执行委托给后台任务：文本处理/落库/释放锁都在任务内）──
        # 整个构建+启动段用 try/finally 收口：连接断开（CancelledError/GeneratorExit）
        # 可能发生在任务启动之前，此时会话锁还握在本请求手里，必须在这里释放，
        # 否则锁要等 TTL（600s）才过期；任务一旦启动，锁所有权移交任务协程。
        task_launched = False
        try:
            llm_client = LLMClient(settings)
            ai_service = AIService(llm_client)

            history: list[dict] = []
            if body.history:
                history = [
                    {"role": h["role"], "content": str(h.get("content", ""))}
                    for h in body.history
                    if isinstance(h, dict) and h.get("role") in ("user", "assistant")
                ]

            # 额外注入：把最近若干轮已生成的图表清单带进上下文，避免 LLM 重复生成同类型图表。
            # 加总量预算：旧实现按"每图 600 字符 × 最多 6 条消息"累加且**无总量上限**，
            # 一个多图答案就能灌进上万字符 —— 这是"免费可省的一层"：先裁状态载荷，再谈摘要。
            chart_summary_note = ""
            if session_id:
                chart_budget = int(getattr(settings, "LEAD_CTX_CHART_SUMMARY_TOKENS", 1200) or 1200)   # token 口径（混排）
                prior_msgs = (
                    await db.execute(
                        select(AIMessage)
                        .where(AIMessage.session_id == session_id)
                        # 次级键 role 与 created_at 同方向（见同文件画布入口的说明）
                        .order_by(AIMessage.created_at.desc(), AIMessage.role.desc())
                        .limit(6)
                    )
                ).scalars().all()
                prior_msgs.reverse()
                chart_summary_note = _build_chart_summary_note(prior_msgs, chart_budget)
                if chart_summary_note:
                    # legacy 路径从 history 里取（role=system）；
                    # Lead 路径通过 LeadContext.extra_context 注入（见下方 _event_factory）
                    history.append({"role": "system", "content": chart_summary_note})

            # Build agent message: inject datasource context if datasource_id is provided
            agent_message = body.message
            if body.datasource_id:
                try:
                    ds_uuid = UUID(body.datasource_id)
                except (ValueError, TypeError):
                    ds_uuid = None
                if ds_uuid:
                    ds_result = await db.execute(
                        select(DataSource).where(
                            DataSource.id == ds_uuid,
                            DataSource.user_id == current_user.id,
                        )
                    )
                    datasource = ds_result.scalar_one_or_none()
                    if datasource:
                        from app.models.datasource import SourceType
                        fields = (datasource.schema_meta.get("fields") or []) if isinstance(datasource.schema_meta, dict) else []
                        columns: list[str] = []
                        field_lines: list[str] = []
                        for f in fields:
                            if isinstance(f, dict):
                                name = f.get("name", "?")
                                dtype = f.get("data_type", "?")
                                cat = f.get("category", "")
                                cat_str = f" [{cat}]" if cat else ""
                                columns.append(name)
                                field_lines.append(f"  - {name} ({dtype}{cat_str})")
                        field_list = "\n".join(field_lines) if field_lines else "（无字段信息）"

                        # 构建 table_ref（列名已在下方注入，无需 sample_sql；严禁 SELECT * 探查）
                        schema_name = duckdb_client.get_schema_name(str(current_user.id), str(datasource.id), datasource.name)
                        if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
                            meta = datasource.schema_meta if isinstance(datasource.schema_meta, dict) else {}
                            table_name = meta.get("table_name", "data")
                            table_ref = f'"{schema_name}".public."{table_name}"'
                        else:
                            table_ref = f'"{schema_name}"."data"'

                        columns_str = ", ".join(columns) if columns else "（无）"
                        _log.info(
                            "[chat] ds_injection name=%s id=%s columns=%d",
                            datasource.name, datasource.id, len(columns),
                        )
                        agent_message = (
                            f"【系统注入：当前已连接数据源】\n"
                            f"数据源名称: {datasource.name}\n"
                            f"数据源 ID: {body.datasource_id}\n"
                            f"总行数: {datasource.row_count or '未知'}\n"
                            f"table_ref（FROM 子句必须原样复制）: {table_ref}\n"
                            f"列名（columns，SQL 中必须加双引号；严禁 SELECT *）: {columns_str}\n"
                            f"字段详情:\n{field_list}\n\n"
                            f"用户问题: {body.message}"
                        )

            # 指标清单注入（所有分析入口共享）：让 Lead/ReAct/Orchestrator 优先引用受治理指标，
            # 而非让 LLM 自由拼 SUM(field)。指标多时只给示例 + 提示调 list_metrics，避免上下文膨胀。
            metrics_ctx_for_lead = ""
            try:
                from app.services.metric_service import format_metrics_context, list_metrics_for_user
                _metrics = await list_metrics_for_user(db, current_user.id)
                metrics_ctx_for_lead = format_metrics_context(_metrics)
                if metrics_ctx_for_lead:
                    if len(metrics_ctx_for_lead) <= 4000:
                        agent_message += f"\n\n【系统注入：平台已定义的受治理业务指标】\n{metrics_ctx_for_lead}\n涉及这些指标的问题必须用 metric_key 引用（可用 list_metrics 查看），不要自行拼 SUM/AVG 口径"
                    else:
                        sample_keys = ", ".join(f'"{m.key}"' for m in _metrics[:5])
                        agent_message += (
                            f"\n\n【系统注入：平台已定义 {len(_metrics)} 个受治理业务指标，无法全量展示；"
                            f"示例 key: {sample_keys}。查询涉及业务指标口径时先调 list_metrics 查看完整清单，"
                            f"再用 metric_key 引用，不要自行拼 SUM/AVG 口径】"
                        )
            except Exception:
                _log.info("metrics context injection skipped", exc_info=True)

            (
                memory_summary,
                memory_covered,
                memory_watermark,
                memory_fail_count,
            ) = await _load_session_memory(db, session_id)

            # 服务端窗口要排除的本条用户消息（已落库，id 已知）
            user_msg_id_for_window = user_msg_model.id if session_id else None
            # 阶段 3：服务端窗口在请求作用域查询一次（快照语义——窗口本就该反映
            # 本轮启动前的会话状态）；factory 是同步函数，不能 await。
            server_window_snapshot = await _load_server_window(
                db, session_id, exclude_msg_id=user_msg_id_for_window
            )

            def _event_factory(db_session: AsyncSession):
                """由后台任务调用（传入任务自己的 db session，请求断开后它仍有效）。"""
                from app.services.agents.lead import LeadAgent, LeadContext
                from app.services.observability import get_observer

                lead_ctx = LeadContext(
                    user_id=str(current_user.id),
                    session_id=str(session_id) if session_id else "",
                    entry="chat",
                    datasource_id=body.datasource_id,
                    history_summary=memory_summary or "",
                    memory_covered=memory_covered,
                    memory_watermark=memory_watermark,
                    memory_fail_count=memory_fail_count,
                    extra_context=chart_summary_note,
                    metrics_ctx=metrics_ctx_for_lead,
                    ui_action=getattr(body, "ui_action", None),
                )
                # 阶段 3：窗口所有权收归服务端——DB 取最近 N 轮（唯一所有者），
                # 前端 history 降级为兜底（仅当 DB 窗口为空时使用，如极端落库延迟）。
                # 前端把本条用户消息一并放进 history（aiChatStore 追加），而 stream 内部
                # 会统一 add_turn 当前消息——这里剔除尾部的本条，避免同一消息在窗口里出现两次。
                history_for_ctx = history
                if (
                    history_for_ctx
                    and history_for_ctx[-1].get("role") == "user"
                    and history_for_ctx[-1].get("content") == body.message
                ):
                    history_for_ctx = history_for_ctx[:-1]
                # 成对对齐：前端按条数 slice 出来的窗口可能从"上一轮的回答"开始，
                # 这类孤立回答丢掉（否则模型开场就看到一个没有来由的答案）。
                aligned_history = _align_history_pairs([
                    {"role": h["role"], "content": str(h.get("content", ""))}
                    for h in history_for_ctx
                    if isinstance(h, dict) and h.get("role") in ("user", "assistant")
                ])
                window_turns = server_window_snapshot or aligned_history
                for h in window_turns:
                    lead_ctx.turns.append(h)
                return _lead_stream_guard(
                    LeadAgent(llm=llm_client, observer=get_observer()).stream(
                        agent_message, ctx=lead_ctx, db_session=db_session
                    )
                )

            # ── 清上一轮残留（必须在"启动执行"**之前**）──────────────────────────
            # 为什么顺序重要：
            #   · 队列模式：worker 是独立进程、已在轮询队列，入队后毫秒级就可能开始写事件；
            #   · 进程内模式：create_task 也会在下一个 await 点被调度执行。
            # 若把清空放在启动之后，新任务刚写入的事件可能立刻被 clear 擦掉——
            # 日志缺开头，续收回放就不完整（这是审查中发现的真实竞态）。
            try:
                get_cache_repository().delete(_chat_text_key(task_sid))
            except Exception:
                _log.debug("chat streamed text cache clear failed", exc_info=True)
            try:
                await event_stream.clear(task_sid)
            except Exception:
                _log.debug("chat event stream clear failed", exc_info=True)

            # ── 启动执行（两条路，由 TASK_QUEUE_ENABLED 选择）────────────────────
            # A) 队列模式：投递到 ARQ，由**独立 worker 进程**执行——API 进程重启不影响
            #    已入队任务；事件仍写同一份事件日志，本连接只读日志（见 _stream_from_event_log）。
            # B) 进程内模式（默认）：asyncio.Task 在本进程执行，行为与历史一致。
            # 队列不可用时自动回退 B，保证可用性不受新组件影响。
            queued = False
            if settings.TASK_QUEUE_ENABLED:
                from app.worker import enqueue_chat_task

                job_id = await enqueue_chat_task(
                    session_id=str(session_id) if session_id else None,
                    lock_token=lock_token,
                    assistant_msg_id=str(assistant_msg_id) if assistant_msg_id else None,
                    task_key=task_key,
                    payload={
                        "user_id": str(current_user.id),
                        "session_id": str(session_id) if session_id else "",
                        "entry": "chat",
                        "datasource_id": body.datasource_id,
                        "history_summary": memory_summary or "",
                        "memory_covered": memory_covered,
                        "memory_watermark": memory_watermark,
                        "memory_fail_count": memory_fail_count,
                        "extra_context": chart_summary_note,
                        "metrics_ctx": metrics_ctx_for_lead,
                        "ui_action": getattr(body, "ui_action", None),
                        "turns": server_window_snapshot,
                        "agent_message": agent_message,
                    },
                )
                queued = job_id is not None
                if not queued:
                    _log.warning(
                        "task_queue_enqueue_failed_fallback_inprocess session=%s", session_id
                    )

            started = queued or chat_registry.start(
                task_sid,
                lambda: _run_chat_task(
                    session_id=session_id,
                    lock_token=lock_token,
                    assistant_msg_id=assistant_msg_id,
                    task_key=task_key,
                    event_factory=_event_factory,
                ),
            )
            task_launched = started
            if not started:
                # 极端竞态：start 时已存在同名运行任务（本轮回并发双发），按 busy 处理
                yield f"data: {json.dumps({'type': 'error', 'message': _SESSION_BUSY_MSG}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                return
        finally:
            # 任务未启动（构建中途异常 / 连接断开取消 / start 竞态被拒）→ 清理本轮占位副作用：
            # 锁还由本请求持有（任务没接手），running 任务态也是本轮写的，一并清掉。
            # 任务已启动 → 锁与任务态归任务协程管，这里什么都不做。
            if not task_launched:
                if lock_token:
                    release_session_lock(session_id, lock_token)
                if task_key:
                    try:
                        get_cache_repository().delete(task_key)
                    except Exception:
                        pass

        # 本连接作为首个订阅者。
        if queued:
            # 队列模式：执行在另一个进程，只能从事件日志读（回放 + 续读直到 done）。
            # max_idle_seconds：worker 中途被杀时日志不会再有事件，靠它兜底退出
            # （LLM 超时 120s、流空闲看门狗 90s，正常任务不会 4 分钟毫无动静）。
            async for line in _stream_from_event_log(task_sid, max_idle_seconds=240.0):
                yield line
        else:
            # 进程内模式：先补发一次文本快照（新任务通常为空），再实时转发任务事件
            async for line in _yield_streamed_text_snapshot(task_sid):
                yield line
            async for line in _yield_subscription_events(task_sid):
                yield line

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.post("/clean")
async def clean_data(
    body: AICleanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    ds = await db.execute(
        select(DataSource).where(
            DataSource.id == body.datasource_id,
            DataSource.user_id == current_user.id,
        )
    )
    if ds.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    schema_name = duckdb_client.get_schema_name(current_user.id, body.datasource_id, datasource.name)
    changes: list[dict[str, Any]] = []

    for rule in body.rules:
        field = rule.get("field", "")
        action = rule.get("action", "")
        if not field or not action:
            continue

        where_clause = _build_clean_where(field, action)
        rule_result = await asyncio.to_thread(
            _query_clean_preview, schema_name, field, action, where_clause
        )
        changes.append(rule_result)

    return SuccessResponse(data={"changes": changes})


@router.post("/clean/apply")
async def apply_clean(
    body: AICleanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """实际执行数据清洗（DELETE/UPDATE），仅支持 CSV/Excel 数据源。"""
    ds_result = await db.execute(
        select(DataSource).where(
            DataSource.id == body.datasource_id,
            DataSource.user_id == current_user.id,
        )
    )
    datasource = ds_result.scalar_one_or_none()
    if datasource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    # 仅支持 CSV/Excel（DuckDB 直接管理的数据）
    from app.models.datasource import SourceType
    if datasource.source_type not in (SourceType.csv, SourceType.excel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "UNSUPPORTED", "message": "清洗仅支持 CSV/Excel 数据源，数据库数据源暂不支持直接清洗"},
        )

    schema_name = duckdb_client.get_schema_name(current_user.id, body.datasource_id, datasource.name)
    results: list[dict[str, Any]] = []
    pg_table_name = "data"

    for rule in body.rules:
        field = rule.get("field", "")
        action = rule.get("action", "")
        if not field or not action:
            continue

        try:
            rule_result = await asyncio.to_thread(
                _execute_clean, schema_name, field, action, pg_table_name
            )
            results.append(rule_result)
        except Exception as e:
            _log.warning("clean apply failed for field=%s action=%s: %s", field, action, e)
            results.append({
                "field": field,
                "action": action,
                "success": False,
                "affected_count": 0,
                "error": str(e),
            })

    return SuccessResponse(data={"results": results})


def _execute_clean(
    schema_name: str, field: str, action: str, table_name: str = "data"
) -> dict[str, Any]:
    """Execute the actual cleaning SQL on DuckDB (synchronous, for asyncio.to_thread)."""
    quoted_field = f'"{field}"'
    table_ref = f'"{schema_name}"."{table_name}"'

    if action == "drop_null":
        sql_count = f'SELECT count(*) FROM {table_ref} WHERE {quoted_field} IS NULL'
        sql_exec = f'DELETE FROM {table_ref} WHERE {quoted_field} IS NULL'
    elif action == "drop_negative":
        sql_count = f'SELECT count(*) FROM {table_ref} WHERE {quoted_field} < 0'
        sql_exec = f'DELETE FROM {table_ref} WHERE {quoted_field} < 0'
    elif action == "fill_mean":
        sql_count = f'SELECT count(*) FROM {table_ref} WHERE {quoted_field} IS NULL'
        avg_rows = duckdb_client.fetchall(
            f'SELECT AVG({quoted_field}) FROM {table_ref} WHERE {quoted_field} IS NOT NULL'
        )
        avg_val = avg_rows[0][0] if avg_rows else None
        if avg_val is None:
            return {"field": field, "action": action, "success": False, "affected_count": 0, "error": "无法计算均值（所有值均为空）"}
        sql_exec = f'UPDATE {table_ref} SET {quoted_field} = {avg_val} WHERE {quoted_field} IS NULL'
    elif action == "fill_median":
        sql_count = f'SELECT count(*) FROM {table_ref} WHERE {quoted_field} IS NULL'
        med_rows = duckdb_client.fetchall(
            f'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {quoted_field}) FROM {table_ref} WHERE {quoted_field} IS NOT NULL'
        )
        med_val = med_rows[0][0] if med_rows else None
        if med_val is None:
            return {"field": field, "action": action, "success": False, "affected_count": 0, "error": "无法计算中位数（所有值均为空）"}
        sql_exec = f'UPDATE {table_ref} SET {quoted_field} = {med_val} WHERE {quoted_field} IS NULL'
    elif action == "fill_mode":
        sql_count = f'SELECT count(*) FROM {table_ref} WHERE {quoted_field} IS NULL'
        mode_rows = duckdb_client.fetchall(
            f"SELECT {quoted_field} FROM {table_ref} WHERE {quoted_field} IS NOT NULL "
            f"GROUP BY {quoted_field} ORDER BY count(*) DESC LIMIT 1"
        )
        mode_val = mode_rows[0][0] if mode_rows else None
        if mode_val is None:
            return {"field": field, "action": action, "success": False, "affected_count": 0, "error": "无法计算众数（所有值均为空）"}
        quoted_val = f"'{mode_val}'" if isinstance(mode_val, str) else str(mode_val)
        sql_exec = f'UPDATE {table_ref} SET {quoted_field} = {quoted_val} WHERE {quoted_field} IS NULL'
    elif action == "fill_ffill":
        sql_count = f'SELECT count(*) FROM {table_ref} WHERE {quoted_field} IS NULL'
        sql_exec = (
            f"UPDATE {table_ref} SET {quoted_field} = ("
            f"  SELECT lag_val FROM ("
            f"    SELECT {quoted_field}, "
            f"      LAST_VALUE({quoted_field}) IGNORE NULLS OVER (ORDER BY rowid) AS lag_val"
            f"    FROM (SELECT rowid, {quoted_field} FROM {table_ref}) sub"
            f"  ) sub2 WHERE sub2.{quoted_field} IS NULL AND sub2.lag_val IS NOT NULL"
            f") WHERE {quoted_field} IS NULL"
        )
    elif action == "standardize_date":
        sql_count = (
            f'SELECT count(*) FROM {table_ref} '
            f'WHERE {quoted_field} IS NOT NULL '
            f"AND NOT regexp_matches({quoted_field}::VARCHAR, '^\\d{{4}}-\\d{{2}}-\\d{{2}}$')"
        )
        return {"field": field, "action": action, "success": True, "affected_count": 0, "message": "日期标准化需要手动处理，已跳过"}
    else:
        return {"field": field, "action": action, "success": False, "affected_count": 0, "error": f"未知清洗动作: {action}"}

    # Use fetchall for thread safety (execute+fetchone outside lock is unsafe)
    cnt_rows = duckdb_client.fetchall(sql_count)
    affected = int(cnt_rows[0][0]) if cnt_rows else 0

    if affected > 0:
        duckdb_client.execute(sql_exec)

    return {"field": field, "action": action, "success": True, "affected_count": affected}


def _build_clean_where(field: str, action: str) -> str:
    """Build WHERE clause fragment for a given clean action."""
    quoted = f'"{field}"'
    if action == "drop_null":
        return f'{quoted} IS NULL'
    elif action == "drop_negative":
        return f'{quoted} < 0'
    elif action == "standardize_date":
        return (
            f'{quoted} IS NOT NULL '
            f"AND NOT regexp_matches({quoted}::VARCHAR, '^\\d{{4}}-\\d{{2}}-\\d{{2}}$')"
        )
    elif action == "fill_mean":
        return f'{quoted} IS NULL'
    else:
        return f'{quoted} IS NULL'


def _query_clean_preview(
    schema_name: str, field: str, action: str, where_clause: str
) -> dict[str, Any]:
    """Execute preview query and count query in DuckDB (synchronous, for asyncio.to_thread)."""
    sql = (
        f'SELECT * FROM "{schema_name}"."data" '
        f"WHERE {where_clause} "
        f"LIMIT 10"
    )
    result = duckdb_client.execute(sql)
    columns = [desc[0] for desc in result.description]
    rows = result.fetchall()
    preview_rows: list[dict] = [dict(zip(columns, row)) for row in rows]
    count_sql = (
        f'SELECT count(*) FROM "{schema_name}"."data" '
        f"WHERE {where_clause}"
    )
    count_result = duckdb_client.execute(count_sql)
    affected_count = count_result.fetchone()[0]
    return {
        "field": field,
        "action": action,
        "affected_count": affected_count,
        "preview_rows": preview_rows,
    }


_SQL_GEN_SYSTEM = """你是 DuckDB SQL 专家。根据用户的问题和数据源字段信息，生成一条 DuckDB SQL 查询。
只输出 SQL，不要任何解释或 markdown 标记。只做 SELECT 不允许 INSERT/UPDATE/DELETE/DROP。
表名为 {table_ref}，字段需用双引号包裹。
注意：WHERE 条件中的具体值直接写在 SQL 中（如 WHERE status = 'Completed'），LIMIT 也直接写数字（如 LIMIT 10），不要使用 ? 占位符。"""


def _sanitize_db_error(raw_error: str) -> str:
    """Convert raw DuckDB/DB errors to user-friendly messages."""
    raw_str = str(raw_error)
    if "INTERNAL" in raw_str or "Internal" in raw_str:
        return "数据库内部错误，可能是 SQL 语法或表结构不兼容导致。请尝试调整查询条件或更换字段。"
    if "Catalog" in raw_str and "does not exist" in raw_str:
        return "查询的表或字段不存在，请确认数据源字段名是否正确。"
    if len(raw_str) > 150:
        return "数据库查询异常，请检查数据源连接或 SQL 语句是否正确。"
    return f"SQL 执行失败: {raw_str}"


# 画布助手工具循环：SQL / 图表配置各自的最多修复次数（同轮自纠错上限）
_CANVAS_MAX_RETRIES = 2


def _extract_sql_block(text: str) -> str:
    """从 LLM 回复中提取 ```sql 代码块内容。"""
    m = re.search(r"```sql\s*\n(.*?)\n```", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_chart_config(text: str) -> dict | None:
    """从 LLM 回复中提取第一个合法图表配置（兼容有无 action 字段）。"""
    for m in re.finditer(r"```json\s*\n(.*?)\n```", text, re.DOTALL):
        try:
            cfg = json.loads(m.group(1))
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        if (
            cfg.get("action") == "apply_chart"
            or (
                isinstance(cfg.get("dimensions"), list)
                and isinstance(cfg.get("measures"), list)
                and isinstance(cfg.get("chart_type"), str)
            )
        ):
            return cfg
    return None

@router.post("/query")
async def ai_query(
    body: AIQueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """AI 自然语言查询数据"""
    if not settings.is_ai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_NOT_CONFIGURED", "message": "请在 .env 配置 OPENAI_API_KEY"},
        )

    # 获取数据源字段信息
    field_info = ""
    table_ref = None
    schema_name = None
    if body.datasource_id:
        ds_result = await db.execute(
            select(DataSource).where(
                DataSource.id == body.datasource_id,
                DataSource.user_id == current_user.id,
            )
        )
        datasource = ds_result.scalar_one_or_none()
        if datasource and datasource.schema_meta:
            fields = (datasource.schema_meta.get("fields") or []) if isinstance(datasource.schema_meta, dict) else []
            field_lines = []
            for f in fields:
                if isinstance(f, dict):
                    name = f.get("name", "")
                    dtype = f.get("data_type", "VARCHAR")
                    field_lines.append(f"{name} ({dtype})")
            if field_lines:
                field_info = "可用字段：\n" + "\n".join(field_lines)
                schema_name = duckdb_client.get_schema_name(current_user.id, body.datasource_id, datasource.name)
                # PostgreSQL/MySQL 源用三部分表名，CSV/Excel 用 "schema"."data"
                from app.models.datasource import SourceType
                if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
                    pg_table = (datasource.schema_meta.get("table_name") or "data") if isinstance(datasource.schema_meta, dict) else "data"
                    table_ref = f'"{schema_name}".public."{pg_table}"'
                    # 首次 ATTACH 后复用连接，确保外部数据库可查（按类型选对应连接器）
                    from app.utils.crypto import decrypt_value, get_encryption_key
                    conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
                    key = get_encryption_key()
                    if key and conn_info.get("password"):
                        conn_info["password"] = decrypt_value(conn_info["password"], key)
                    conn_info["user"] = conn_info.get("username", "root" if datasource.source_type == SourceType.mysql else "postgres")
                    conn_info["database"] = conn_info.get("db_name", "")
                    if datasource.source_type == SourceType.mysql:
                        from app.connectors.mysql_connector import mysql_connector as mysql_conn
                        attach_sql = mysql_conn.get_attach_sql(conn_info, schema_name)
                    else:
                        from app.connectors.postgres_connector import postgres_connector as pg_conn
                        attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
                    duckdb_client.ensure_attached(schema_name, attach_sql)
                else:
                    table_ref = f'"{schema_name}"."data"'

    if not table_ref:
        return SuccessResponse(
            data={
                "answer": body.question,
                "sql": None,
                "data": None,
                "error": "未指定数据源或数据源无可用字段",
            }
        )

    # 让 AI 生成 SQL
    llm_client = LLMClient(settings)
    sql_prompt = _SQL_GEN_SYSTEM.format(table_ref=table_ref)
    try:
        sql_response = await llm_client.complete(
            [
                {"role": "system", "content": sql_prompt},
                {"role": "user", "content": f"{field_info}\n\n用户问题：{body.question}"},
            ],
            temperature=0.1,
            max_tokens=500,
        )
    except (AINotConfiguredError, AIUpstreamError) as e:
        _log.warning("AI query LLM error: %s", e)
        return SuccessResponse(
            data={
                "answer": body.question,
                "sql": None,
                "data": None,
                "error": f"LLM 调用失败: {str(e)}",
            }
        )

    _log.info("AI query raw SQL response: %s", sql_response)

    # 清洗 SQL（去掉可能的 markdown 标记）
    sql = sql_response.strip()
    _log.info("AI query cleaned SQL: %s", sql)
    if sql.startswith("```"):
        sql = sql.split("\n", 1)[1] if "\n" in sql else sql[3:]
    if sql.endswith("```"):
        sql = sql[:-3]
    sql = sql.strip().rstrip(";").strip()

    # P0 安全修复：走完整三层防护（不仅看 SELECT 开头）
    from app.services.sql_guard import sql_guard
    guard_result = sql_guard.full_check(body.question, sql)
    if not guard_result.allowed:
        _log.warning(
            "AI query SQL 被 L3 拦截",
            layer=guard_result.layer,
            reason=guard_result.reason,
            sql_preview=sql[:200],
        )
        return SuccessResponse(
            data={
                "answer": body.question,
                "sql": sql,
                "data": None,
                "error": f"SQL 未通过安全检查: {guard_result.reason}",
                "blocked_layer": guard_result.layer,
            }
        )
    # L3 内部已自动 LIMIT 注入，使用 sanitized_sql
    sql = guard_result.sanitized_sql or sql

    # 执行查询：统一走共享工具 QueryDatasourceTool（L3 防护 + ATTACH + 列名解析 + 失败 hint）
    try:
        from app.services.agent_tools import QueryDatasourceTool
        tool_result = await QueryDatasourceTool().execute(
            datasource_id=body.datasource_id,
            sql=sql,
            user_id=str(current_user.id),
            db_session=db,
        )
        parsed = json.loads(tool_result)
        if "error" in parsed:
            err_msg = parsed.get("error", "未知错误")
            return SuccessResponse(
                data={
                    "answer": body.question,
                    "sql": sql,
                    "data": None,
                    "error": f"查询执行失败: {err_msg}",
                    "hint": parsed.get("hint"),
                }
            )
        columns = parsed.get("columns", [])
        data_rows = parsed.get("rows", [])
    except Exception as e:
        return SuccessResponse(
            data={
                "answer": body.question,
                "sql": sql,
                "data": None,
                "error": f"查询执行失败: {str(e)}",
            }
        )

    return SuccessResponse(
            data={
                "answer": body.question,
                "sql": sql,
                "data": None,
                "error": f"查询执行失败: {str(e)}",
            }
        )

    return SuccessResponse(
        data={
            "answer": body.question,
            "sql": sql,
            "data": {
                "columns": columns,
                "rows": data_rows[:50],  # 最多返回50行
            },
            "error": None,
        }
    )


_ALLOWED_STYLES = frozenset({"professional", "casual", "concise", "academic"})

_log = logging.getLogger("lvco.ai_api")


def _json_safe_val(value: Any) -> Any:
    """Convert Python date/datetime/Decimal to JSON-serializable types."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


@router.post("/insights")
async def generate_insights(
    body: InsightsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    ds_result = await db.execute(
        select(DataSource).where(
            DataSource.id == body.datasource_id,
            DataSource.user_id == current_user.id,
        )
    )
    datasource = ds_result.scalar_one_or_none()
    if datasource is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    schema_name = duckdb_client.get_schema_name(current_user.id, body.datasource_id, datasource.name)
    # 构建正确的表引用
    from app.models.datasource import SourceType
    if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
        pg_table = (datasource.schema_meta.get("table_name") or "data") if isinstance(datasource.schema_meta, dict) else "data"
        table_ref = f'"{schema_name}".public."{pg_table}"'
        # 首次 ATTACH 后复用连接（按类型选对应连接器）
        from app.utils.crypto import decrypt_value, get_encryption_key
        conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
        key = get_encryption_key()
        if key and conn_info.get("password"):
            conn_info["password"] = decrypt_value(conn_info["password"], key)
        conn_info["user"] = conn_info.get("username", "root" if datasource.source_type == SourceType.mysql else "postgres")
        conn_info["database"] = conn_info.get("db_name", "")
        if datasource.source_type == SourceType.mysql:
            from app.connectors.mysql_connector import mysql_connector as mysql_conn
            attach_sql = mysql_conn.get_attach_sql(conn_info, schema_name)
        else:
            from app.connectors.postgres_connector import postgres_connector as pg_conn
            attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
        duckdb_client.ensure_attached(schema_name, attach_sql)
    else:
        table_ref = f'"{schema_name}"."data"'

    try:
        query_results = await asyncio.to_thread(_execute_insights_query, table_ref, body.query_config)
    except Exception as e:
        _log.warning("Insights query execution failed: %s", e)
        return SuccessResponse(data={"insights": []})

    try:
        ai_service = AIService(LLMClient(settings))
        insights = await ai_service.generate_insights(query_results[:500], body.query_config)
    except (AINotConfiguredError, AIUpstreamError) as e:
        _log.warning("LLM insights fallback: %s", e)
        insights = []

    return SuccessResponse(data={"insights": insights})


@router.post("/polish")
async def polish_text(
    body: PolishRequest,
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    style = body.style if body.style in _ALLOWED_STYLES else "professional"

    try:
        ai_service = AIService(LLMClient(settings))
        result = await ai_service.polish_text(body.text, style)
    except (AINotConfiguredError, AIUpstreamError) as e:
        _log.warning("LLM polish fallback: %s", e)
        result = {"original": body.text, "polished": body.text, "style": style}

    return SuccessResponse(data=result)


def _execute_insights_query(table_ref: str, query_config: dict) -> list[dict]:
    dimensions: list[str] = query_config.get("dimensions") or []
    measures: list[dict] = query_config.get("measures") or []
    filters: list[dict] = query_config.get("filters") or []

    select_parts: list[str] = []
    result_columns: list[str] = []

    for dim in dimensions:
        dim_str = str(dim)
        select_parts.append(f'"{dim_str}"')
        result_columns.append(dim_str)

    for m in measures:
        if not isinstance(m, dict):
            continue
        field = m.get("field") or m.get("name") or ""
        agg = (m.get("agg") or "SUM").upper()
        alias = f"{agg.lower()}_{field}"
        select_parts.append(f'{agg}("{field}") AS "{alias}"')
        result_columns.append(field)

    if not select_parts:
        select_parts.append("*")
        result_columns.append("*")

    where_clause = ""
    params: list[Any] = []
    if filters:
        conditions: list[str] = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            field = f.get("field") or ""
            op = f.get("op") or "eq"
            value = f.get("value")
            if op == "eq":
                conditions.append(f'"{field}" = ?')
                params.append(value)
            elif op == "neq":
                conditions.append(f'"{field}" != ?')
                params.append(value)
            elif op == "gt":
                conditions.append(f'"{field}" > ?')
                params.append(value)
            elif op == "gte":
                conditions.append(f'"{field}" >= ?')
                params.append(value)
            elif op == "lt":
                conditions.append(f'"{field}" < ?')
                params.append(value)
            elif op == "lte":
                conditions.append(f'"{field}" <= ?')
                params.append(value)
            elif op == "like":
                conditions.append(f'"{field}" LIKE ?')
                params.append(value)
            elif op == "in":
                if isinstance(value, list) and value:
                    placeholders = ", ".join(["?"] * len(value))
                    conditions.append(f'"{field}" IN ({placeholders})')
                    params.extend(value)
            elif op == "between":
                if isinstance(value, list) and len(value) == 2:
                    conditions.append(f'"{field}" BETWEEN ? AND ?')
                    params.extend(value)
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

    group_by = ""
    if dimensions:
        group_by = "GROUP BY " + ", ".join(f'"{d}"' for d in dimensions)

    # 按第一个度量降序排列，确保取前 N 名的数据给 AI 分析
    order_by = ""
    if measures and measures[0]:
        first_m = measures[0]
        if isinstance(first_m, dict):
            alias = f'{(first_m.get("agg") or "SUM").lower()}_{first_m.get("field") or first_m.get("name") or ""}'
            order_by = f'ORDER BY "{alias}" DESC'

    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {table_ref} "
        f"{where_clause} "
        f"{group_by} "
        f"{order_by} "
        f"LIMIT 500"
    ).strip()

    rows_raw = duckdb_client.fetchall(sql, params if params else None)
    rows: list[dict[str, Any]] = []
    for row in rows_raw:
        row_dict: dict[str, Any] = {}
        for i, col in enumerate(result_columns):
            row_dict[col] = row[i] if i < len(row) else None
        rows.append(row_dict)

    # Debug: log first 3 rows to verify data integrity
    _log.info("Insights query returned %d rows, SQL: %s", len(rows), sql)
    if rows:
        _log.info("Insights TOP 3: %s", json.dumps(rows[:3], ensure_ascii=False, default=str))

    return rows


@router.post("/canvas/chat")
async def canvas_ai_chat(
    request: Request,
    body: CanvasChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Canvas-specific AI chat: understands canvas context, queries data, suggests charts."""
    if not settings.is_ai_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_NOT_CONFIGURED", "message": "请配置 OPENAI_API_KEY"},
        )

    # Get datasource info (optional — only if datasource_id provided)
    datasource = None
    table_ref = ""
    field_info = ""
    if body.datasource_id:
        try:
            ds_uuid = uuid.UUID(body.datasource_id)
        except (ValueError, TypeError):
            ds_uuid = None
        if ds_uuid:
            ds_result = await db.execute(
                select(DataSource).where(
                    DataSource.id == ds_uuid,
                    DataSource.user_id == current_user.id,
                )
            )
            datasource = ds_result.scalar_one_or_none()
            if not datasource:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "NOT_FOUND", "message": "数据源不存在"},
                )

        # Build table reference and field info
        schema_name = duckdb_client.get_schema_name(current_user.id, datasource.id, datasource.name)

        fields = (datasource.schema_meta.get("fields") or []) if isinstance(datasource.schema_meta, dict) else []
        field_lines = []
        for f in fields:
            if isinstance(f, dict):
                name = f.get("name", "")
                dtype = f.get("data_type", "VARCHAR")
                cat = f.get("category", "dimension")
                field_lines.append(f"- {name} ({dtype}, {cat})")
        field_info = "数据源字段：\n" + "\n".join(field_lines) if field_lines else ""

        # Build table ref
        from app.models.datasource import SourceType

        if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
            pg_table = (datasource.schema_meta.get("table_name") or "data") if isinstance(datasource.schema_meta, dict) else "data"
            table_ref = f'"{schema_name}".public."{pg_table}"'
            # 首次 ATTACH 后复用连接（按类型选对应连接器）
            from app.utils.crypto import decrypt_value, get_encryption_key

            conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
            key = get_encryption_key()
            if key and conn_info.get("password"):
                conn_info["password"] = decrypt_value(conn_info["password"], key)
            conn_info["user"] = conn_info.get("username", "root" if datasource.source_type == SourceType.mysql else "postgres")
            conn_info["database"] = conn_info.get("db_name", "")
            if datasource.source_type == SourceType.mysql:
                from app.connectors.mysql_connector import mysql_connector as mysql_conn

                attach_sql = mysql_conn.get_attach_sql(conn_info, schema_name)
            else:
                from app.connectors.postgres_connector import postgres_connector as pg_conn

                attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
            duckdb_client.ensure_attached(schema_name, attach_sql)
        else:
            table_ref = f'"{schema_name}"."data"'

    # Build canvas context
    canvas_ctx = ""
    if body.canvas_context:
        blocks = body.canvas_context.get("blocks", [])
        if blocks:
            canvas_ctx = "\n当前画布已有以下图表：\n"
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "chart":
                    bid = b.get("id") or ""
                    canvas_ctx += f"- id={bid} {b.get('title', '图表')} (类型: {b.get('chartType', '?')}, 维度: {b.get('dimensions', [])}, 度量: {b.get('measures', [])})\n"
        current_cfg = body.canvas_context.get("currentConfig")
        if current_cfg:
            dims = current_cfg.get("dimensions", [])
            meas = current_cfg.get("measures", [])
            ct = current_cfg.get("chartType", "")
            if dims or meas:
                canvas_ctx += f"\n当前正在编辑的图表配置：维度={dims}, 度量={meas}"
                if ct:
                    canvas_ctx += f"，当前选中图表类型={ct}（仅供参考，请根据数据特征自主选择最合适的图表类型）"
                canvas_ctx += "\n"
        # 布局感知（v2）：附加各块坐标，LLM 据此判断画布是否整齐、是否需要 arrange_layout
        _coord_blocks = [b for b in (body.canvas_context.get("blocks") or []) if isinstance(b, dict) and b.get("type") == "chart" and b.get("x") is not None]
        if len(_coord_blocks) >= 2:
            canvas_ctx += (
                f"\n画布当前有 {len(_coord_blocks)} 个图表块，布局坐标如下（x,y 为左上角，width×height 为尺寸）：\n"
            )
            for b in _coord_blocks:
                canvas_ctx += (
                    f"- id={b.get('id') or b.get('block_id') or '?'} "
                    f"pos=({b.get('x')},{b.get('y')}) "
                    f"size={b.get('width')}×{b.get('height')}\n"
                )
            canvas_ctx += (
                "若发现块位置重叠或参差不齐，可在完成落块后调用 arrange_layout 工具自动整理为报告式布局。\n"
            )
        # 即使没有选中图表块，也告知 AI 可以根据字段自由推荐图表
        if not canvas_ctx.strip():
            canvas_ctx = "当前画布为空，你可以根据数据源字段自由推荐图表配置。\n"

    # 组装 user_msg：表引用 + 字段信息 + 画布上下文，交给主导 Agent 统一编排
    canvas_tools_note = (
        "\n（你可以在画布上直接搭报告：用 add_text_block 写标题/叙事，"
        "add_chart_block 建图，update_chart_block/remove_block 改删已有块。）"
    )

    def _canvas_ds_injection(ds) -> str:
        """构建与 AGENT_SYSTEM「情况A」匹配的数据源注入（含 table_ref / 字段）。

        必须使用【系统注入：当前已连接数据源】前缀，否则 LLM 会判定为「未注入」，
        从而反复调用 list_datasources 而无法推进到查询。
        """
        from app.models.datasource import SourceType as _ST

        meta = ds.schema_meta if isinstance(ds.schema_meta, dict) else {}
        fields = meta.get("fields") or []
        field_lines: list[str] = []
        col_names: list[str] = []
        for f in fields:
            if isinstance(f, dict):
                fn = f.get("name")
                if not fn:
                    continue
                col_names.append(fn)
                ft = f.get("data_type", "?")
                fc = f.get("category", "")
                field_lines.append(f"  - {fn} ({ft}{' [' + fc + ']' if fc else ''})")
        # 限制注入字段数量，避免 user_msg 超过 L1 长度上限（2000），
        # 同时保证末尾的用户问题不会被截断。
        _MAX_INJECTED_FIELDS = 25
        if len(field_lines) > _MAX_INJECTED_FIELDS:
            field_lines = field_lines[:_MAX_INJECTED_FIELDS] + [
                f"  ...（共 {len(fields)} 个字段，仅展示前 {_MAX_INJECTED_FIELDS} 个）"
            ]
        schema_name = duckdb_client.get_schema_name(str(current_user.id), str(ds.id), ds.name)
        if ds.source_type in (_ST.postgresql, _ST.mysql):
            tbl = meta.get("table_name", "data")
            table_ref = f'"{schema_name}".public."{tbl}"'
        else:
            table_ref = f'"{schema_name}"."data"'
        return (
            f"【系统注入：当前已连接数据源】\n"
            f"数据源名称: {ds.name}\n"
            f"数据源 ID: {ds.id}\n"
            f"table_ref（FROM 子句必须原样复制）: {table_ref}\n"
            f"列名（SQL 中必须加双引号）: {', '.join(col_names) or '（无）'}\n"
            f"字段详情:\n{chr(10).join(field_lines) or '（无字段信息）'}"
        )

    if datasource:
        user_msg_parts = [_canvas_ds_injection(datasource)]
    else:
        # 未绑定数据源但平台已有：全部注入，让 LLM 直接基于用户问题选择查询（而非反复 list）
        from app.repositories.datasource_repository import SQLAlchemyDataSourceRepository

        try:
            ds_list, _ = await SQLAlchemyDataSourceRepository(db).list_datasources(
                current_user.id, page=1, page_size=50,
                source_type=None, status=None, search=None,
            )
            user_msg_parts = (
                [_canvas_ds_injection(ds) for ds in ds_list]
                if ds_list
                else ["当前未连接数据源，你可以与用户就其需求自由对话。"]
            )
        except Exception:
            user_msg_parts = ["当前未连接数据源，你可以与用户就其需求自由对话。"]

    # 注入指标清单：让 Planner 优先引用命名指标（metric key）而非裸字段聚合，
    # 从而统一口径、支持随指标定义联动刷新。同时保留给 LeadContext.metrics_ctx
    # （决策器 / answer 分支直接消费，避免简单问答把裸字段当指标名）。
    metrics_ctx_for_lead = ""
    try:
        from app.services.metric_service import format_metrics_context, list_metrics_for_user

        metrics = await list_metrics_for_user(db, current_user.id)
        metrics_ctx_for_lead = format_metrics_context(metrics)
        if metrics_ctx_for_lead:
            user_msg_parts.append(metrics_ctx_for_lead)
    except Exception:
        _log.info("metrics context injection skipped", exc_info=True)

    if field_info:
        user_msg_parts.append(field_info)
    if canvas_ctx:
        user_msg_parts.append(canvas_ctx.strip())
    user_msg_parts.append(f"用户问题：{body.message}")
    user_msg = "\n".join(user_msg_parts) + canvas_tools_note

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    # 画布归属校验：canvas_id 存在时确认画布属于当前用户（未传 = 新画布草稿，跳过绑定）。
    # 放在 SSE 生成器之外，归属非法时直接返回 404，避免流中途抛错。
    canvas_uid: uuid.UUID | None = None
    canvas_owner_ok = False
    if body.canvas_id:
        try:
            canvas_uid = uuid.UUID(body.canvas_id)
        except (ValueError, TypeError):
            canvas_uid = None
        if canvas_uid:
            cvs = (
                await db.execute(
                    select(Canvas).where(
                        Canvas.id == canvas_uid, Canvas.user_id == current_user.id
                    )
                )
            ).scalar_one_or_none()
            if cvs is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "NOT_FOUND", "message": "画布不存在"},
                )
            canvas_owner_ok = True

    async def _consume_canvas_task(gen, task_sid: str):
        """消费任务体的 async generator：文本快照 + 事件广播给订阅者。"""
        acc = ""
        async for ev in gen:
            if ev.get("type") == "message" and ev.get("delta"):
                acc += ev["delta"]
                chat_registry.update_text(task_sid, acc)
            chat_registry.publish(task_sid, ev)

    async def _canvas_chat_task(session_id: uuid.UUID | None, user_msg_id, assistant_msg_id, lock_token):
        """后台任务体：Agent 执行 + 事件翻译（yield dict，由连接段 publish 给订阅者）。
        断线不影响本任务：落库/释放会话锁/清任务态都在这里完成（请求级连接已不存活）。"""
        full_content = ""
        canvas_actions_count = 0
        stream_degraded = False  # 事件流里出现过的降级标记，随流尾唯一的 done 下发
        db = async_session_factory()
        try:
            # ---- 3. 加载历史（阶段 3：服务端窗口，与聊天入口共用同一所有者） ----
            history = await _load_server_window(
                db, session_id, exclude_msg_id=user_msg_id, max_rounds=None
            ) if session_id else []

            # ---- 4. Agent 流式执行 ----
            # ── 主导 Agent 事件流（旧双路径已移除，无降级兜底）──
            (
                memory_summary,
                memory_covered,
                memory_watermark,
                memory_fail_count,
            ) = await _load_session_memory(db, session_id)

            from app.services.agents.lead import LeadAgent, LeadContext
            from app.services.observability import get_observer

            lead_ctx = LeadContext(
                user_id=str(current_user.id),
                session_id=str(session_id) if session_id else "",
                entry="canvas",
                canvas_id=body.canvas_id,
                datasource_id=str(datasource.id) if datasource else body.datasource_id,
                history_summary=memory_summary or "",
                memory_covered=memory_covered,
                memory_watermark=memory_watermark,
                memory_fail_count=memory_fail_count,
                metrics_ctx=metrics_ctx_for_lead,
                ui_action=getattr(body, "ui_action", None),
            )
            for h in history:
                if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                    lead_ctx.turns.append({"role": h["role"], "content": str(h.get("content", ""))})
            event_iter = _lead_stream_guard(
                LeadAgent(
                    llm=LLMClient(settings),
                    observer=get_observer(),
                    extra_plannable_tools=CANVAS_ALLOWED_TOOL_NAMES,
                ).stream(user_msg, ctx=lead_ctx, db_session=db)
            )

            async for event in _idle_guard(event_iter, label="canvas_chat"):
                ev_type = event.get("type")
                if ev_type == "text":
                    delta = event.get("content", "")
                    full_content += delta
                    yield ({"type": "message", "delta": delta})
                elif ev_type == "plan":
                    # entry=canvas 与对话入口（entry=chat, mode=orchestrator）区分，
                    # 观测方可从 SSE 直接识别规划来源；steps 保留工具名与目标
                    steps = (event.get("plan") or {}).get("steps", [])
                    yield ({"type": "plan", "entry": "canvas", "mode": "canvas_orchestrator",
                                "steps": [{"tool": s.get("tool"), "goal": s.get("goal")} for s in steps if isinstance(s, dict)]})
                elif ev_type == "status":
                    yield ({"type": "status", "message": event.get("message", ""),
                                "phase": event.get("phase"),
                                "degradation": event.get("degradation")})
                elif ev_type == "tool_call":
                    name = event.get("name", "")
                    args = event.get("args", {}) or {}
                    yield ({"type": "tool_call", "name": name, "args": args})
                elif ev_type == "tool_result":
                    name = event.get("name", "")
                    result_raw = event.get("result", "")
                    yield ({"type": "tool_result", "name": name, "result": result_raw})
                    # 画布工具返回内嵌 canvas_action：提取后转发为独立 SSE 事件，
                    # 前端据此实时落块。画布白名单不含 render_chart，无需（也不可能）兜底合成。
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else {}
                        action = r.get("canvas_action") if isinstance(r, dict) else None
                        if isinstance(action, dict):
                            canvas_actions_count += 1
                            yield ({"type": "canvas_action", **action})
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif ev_type == "intent":
                    yield ({"type": "intent", "intent": event.get("intent"),
                                "confidence": event.get("confidence"),
                                "needs_plan": event.get("needs_plan"),
                                "degraded": event.get("degraded")})
                elif ev_type == "decision":
                    yield ({"type": "decision", "round": event.get("round"),
                                "action": event.get("action"),
                                "tool": event.get("tool"), "reason": event.get("reason"),
                                "degraded": event.get("degraded")})
                elif ev_type == "progress":
                    yield ({"type": "progress", "round": event.get("round"),
                                "index": event.get("index"),
                                "total": event.get("total"), "title": event.get("title"),
                                "status": event.get("status"), "note": event.get("note"),
                                "tool": event.get("tool")})
                elif ev_type == "report":
                    if not full_content.strip():
                        report_delta = event.get("content", "")
                        if report_delta:
                            full_content += report_delta
                            yield ({"type": "message", "delta": report_delta})
                elif ev_type == "memory_saved":
                    yield ({"type": "memory_saved", "chars": event.get("chars", 0)})
                elif ev_type == "error":
                    yield ({"type": "error", "message": event.get("message", "服务异常")})
                elif ev_type == "done":
                    # 不下发：done 统一在流尾补发一次（见下方第 5 步之后）。
                    # 原因：事件流里可能混入子执行器的内部 done，提前下发会让前端
                    # 在子任务落块完就认为本轮结束（解锁输入、清 streaming），出现"一次请求两个 done"。
                    stream_degraded = bool(event.get("degraded", stream_degraded))
                elif ev_type == "compressed_history":
                    # 压缩记忆回流：持久化到会话级记忆，下一轮开始时重新注入上下文
                    await _save_memory(db, session_id, event)

            # ---- 5. 保存助手消息：更新占位行为最终内容，并清除 Redis 任务态 ----
            # 即使 LLM 全程只调用工具（full_content 空），只要有实际画布动作，
            # 也生成一份兜底文案，保证下次加载历史时链路完整。
            save_content = full_content.strip()
            if not save_content and canvas_actions_count > 0:
                save_content = f"本次分析通过画布工具完成：已在画布执行 {canvas_actions_count} 次落块操作，请查看画布内容与工作台执行记录。"
            # 占位行由连接段创建（请求级 session），任务体经 task_db 重新加载后更新
            assistant_row = await db.get(AIMessage, assistant_msg_id) if assistant_msg_id else None
            if assistant_row is not None:
                assistant_row.content = save_content
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                # 任务正常结束：清除 running 标记，避免误报"未完成"
                try:
                    get_cache_repository().delete(f"ai:canvas:task:{session_id}")
                except Exception:
                    pass

            # ---- 6. 统一下发唯一的 done：前端据此结束"思考中"并解锁输入 ----
            yield ({"type": "done", "degraded": stream_degraded})

        except AINotConfiguredError:
            yield ({"type": "error", "message": "AI 未配置"})
        except AIUpstreamError as e:
            yield ({"type": "error", "message": str(e)})
        except Exception as e:
            _log.exception("Canvas AI chat error")
            yield ({"type": "error", "message": f"服务异常: {str(e)}"})
        finally:
            # 兜底：客户端断开（CancelledError/GeneratorExit）或异常中断时，
            # 把已产出的文本补存进占位行，并将 Redis 任务态标记为 interrupted。
            am = await db.get(AIMessage, assistant_msg_id) if assistant_msg_id else None
            if am is not None:
                try:
                    if not am.content and full_content.strip():
                        am.content = full_content.strip()
                    await db.commit()
                except Exception:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                task_key = f"ai:canvas:task:{session_id}"
                try:
                    repo = get_cache_repository()
                    if repo.exists(task_key):
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
            # 会话锁：正常结束/异常/断线都走这里释放（TTL 仅作进程崩溃的兜底）
            release_session_lock(session_id, lock_token)
            await db.close()
    async def event_generator():
        """画布助手 SSE：任务后台化（registry）+ resume 续收。"""
        # 画布动作计数器：工具型对话即使 LLM 不落纯文本，
        # 也能据此生成兜底 assistant 消息，保证会话链路完整。
        session_id: uuid.UUID | None = None
        lock_token: str | None = None
        # 必须先于 try 初始化：抢锁失败会提前 return，此时 finally 里的 assistant_msg 还没被赋值
        assistant_msg: AIMessage | None = None
        task_sid = ""
        # resume：带 session_id 且有运行中任务 → 不重发消息，直接续收
        if getattr(body, "resume", False) and body.session_id:
            task_sid = str(body.session_id)
            rt = chat_registry.get(task_sid)
            if rt is None:
                yield _sse({"type": "status", "message": "后台任务已结束，画布内容以画布为准", "resumed": True, "finished": True})
                yield _sse({"type": "done"})
                return
            yield _sse({"type": "status", "message": "已恢复连接，正在续收后台任务", "resumed": True})
            if rt.full_text:
                yield _sse({"type": "message", "delta": rt.full_text})
            async for ev in chat_registry.subscribe(task_sid):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
            return
        task_launched = False
        try:
            # ---- 1. 解析 / 创建会话（画布按 canvas_id 隔离 + 画布内多会话） ----
            if body.session_id:
                try:
                    sid_uuid = uuid.UUID(body.session_id)
                except (ValueError, TypeError):
                    sid_uuid = None
                if sid_uuid:
                    sess_q = select(AISession).where(
                        AISession.id == sid_uuid,
                        AISession.user_id == current_user.id,
                    )
                    if canvas_owner_ok:
                        # 已绑定画布：只允许复用属于当前画布的 canvas 会话，防止跨画布/跨入口串记忆
                        sess_q = sess_q.where(
                            AISession.entry == "canvas",
                            AISession.canvas_id == canvas_uid,
                        )
                    sess = (await db.execute(sess_q)).scalar_one_or_none()
                    if sess:
                        session_id = sess.id

            # 无有效 session_id：画布 1:1 —— 复用该画布「唯一（最近）会话」；
            # new_session=true 时清空该会话历史重新开始，不新建第二条画布会话。
            # 草稿（无 canvas_id）场景保持新建。
            if session_id is None and body.message.strip():
                if canvas_owner_ok and canvas_uid is not None:
                    latest = (
                        await db.execute(
                            select(AISession)
                            .where(
                                AISession.user_id == current_user.id,
                                AISession.entry == "canvas",
                                AISession.canvas_id == canvas_uid,
                            )
                            .order_by(AISession.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if latest is not None:
                        session_id = latest.id
                        if body.new_session:
                            # 清空重来：删除该会话全部消息，复用同一会话 id
                            await db.execute(
                                delete(AIMessage).where(AIMessage.session_id == session_id)
                            )
                            await db.flush()
                            # 通知前端已重置（同一画布会话），避免前端残留旧引用
                            yield _sse({
                                "type": "session_created",
                                "session_id": str(session_id),
                                "reset": True,
                            })
                    else:
                        sess = AISession(
                            user_id=current_user.id,
                            entry="canvas",
                            canvas_id=canvas_uid,
                            title="画布对话",
                        )
                        db.add(sess)
                        await db.flush()
                        await db.refresh(sess)
                        session_id = sess.id
                        yield _sse({
                            "type": "session_created",
                            "session_id": str(session_id),
                        })
                else:
                    sess = AISession(
                        user_id=current_user.id,
                        entry="canvas",
                        canvas_id=canvas_uid,
                        title="画布对话",
                    )
                    db.add(sess)
                    await db.flush()
                    await db.refresh(sess)
                    session_id = sess.id
                    yield _sse({
                        "type": "session_created",
                        "session_id": str(session_id),
                    })

            # ---- 1.5 会话级并发锁：同一会话同一时刻只允许一轮在跑 ----
            # 抢不到直接拒；此时还没落消息，不会留下"没人回答"的僵尸消息。
            if session_id:
                lock_token = acquire_session_lock(session_id)
                if lock_token is None:
                    yield _sse({"type": "error", "message": _SESSION_BUSY_MSG})
                    yield _sse({"type": "done"})
                    return

            # ---- 2. 保存用户消息 + 占位 assistant 行（立即落库：断流/切页也不丢） ----
            current_user_msg_id = None
            if session_id:
                user_msg_model = AIMessage(
                    session_id=session_id,
                    role=AIMessageRole.user,
                    content=body.message,
                )
                db.add(user_msg_model)
                # Auto-title
                sess_result = await db.execute(
                    select(AISession).where(AISession.id == session_id)
                )
                sess = sess_result.scalar_one_or_none()
                if sess and sess.title in (None, "新对话", "画布对话"):
                    title = body.message[:30] + ("..." if len(body.message) > 30 else "")
                    sess.title = title
                    db.add(sess)
                # 占位 assistant 行：先落库标记"生成中"，正常结束/中断时统一更新内容
                assistant_msg = AIMessage(
                    session_id=session_id,
                    role=AIMessageRole.assistant,
                    content="",
                )
                db.add(assistant_msg)
                # 会话 + 用户消息 + 占位行一次性提交，保证客户端断开后历史仍可读到
                await db.commit()
                current_user_msg_id = user_msg_model.id
                # Redis 任务态：标记 running（供"上次未完成"提示）
                try:
                    get_cache_repository().set(
                        f"ai:canvas:task:{session_id}",
                        json.dumps({
                            "status": "running",
                            "started_at": datetime.now(timezone.utc).isoformat(),
                        }),
                        ttl=settings.redis_ttl,
                    )
                except Exception:
                    _log.debug("canvas task running cache set failed", exc_info=True)

            assistant_msg_id = assistant_msg.id if assistant_msg is not None else None
            user_msg_id = user_msg_model.id if (session_id and body.message.strip()) else None
            task_sid = str(session_id) if session_id else f"anon-{uuid.uuid4()}"
            task_launched = chat_registry.start(
                task_sid,
                lambda: _consume_canvas_task(_canvas_chat_task(session_id, user_msg_id, assistant_msg_id, lock_token), task_sid),
            )
            if not task_launched:
                yield _sse({"type": "error", "message": _SESSION_BUSY_MSG})
                yield _sse({"type": "done"})
                return
            # 本连接作为首个订阅者：补发文本快照，再实时转发任务事件
            rt = chat_registry.get(task_sid)
            if rt and rt.full_text:
                yield _sse({"type": "message", "delta": rt.full_text})
            async for ev in chat_registry.subscribe(task_sid):
                yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
        finally:
            # 任务未启动（构建中途异常/竞态被拒）→ 清理本轮副作用；已启动 → 归任务协程管
            if not task_launched:
                if lock_token:
                    release_session_lock(session_id, lock_token)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )