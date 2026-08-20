import asyncio
import json
import logging
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.config import settings
from app.core.database import get_db
from app.core.duckdb_client import duckdb_client
from app.core.limiter import limiter
from app.models.ai_message import AIMessage, AIMessageRole
from app.models.ai_session import AISession
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
from app.services.ai_prompts import CANVAS_AGENT_SYSTEM, CANVAS_SYSTEM
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient

router = APIRouter(prefix="/ai", tags=["AI助手"])


@router.get("/sessions")
async def list_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    result = await db.execute(
        select(AISession)
        .where(AISession.user_id == current_user.id)
        .order_by(AISession.created_at.desc())
    )
    items = list(result.scalars().all())
    return SuccessResponse(
        data=[AISessionResponse.model_validate(s).model_dump(mode="json", by_alias=True) for s in items]
    )


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: AISessionCreate | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    payload = body or AISessionCreate()
    session = AISession(
        user_id=current_user.id,
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
        .order_by(AIMessage.created_at.asc())
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
        .order_by(AIMessage.created_at.asc())
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

        # Create session if not exists
        if session is None and body.message.strip():
            session = AISession(
                user_id=current_user.id,
                title="新对话",
            )
            db.add(session)
            await db.flush()
            await db.refresh(session)

        session_id = session.id if session else None

        # Save user message
        if session_id:
            user_msg_model = AIMessage(
                session_id=session_id,
                role=AIMessageRole.user,
                content=body.message,
            )
            db.add(user_msg_model)
            await db.flush()

            # Auto-title
            if session.title in (None, "新对话"):
                title = body.message[:30] + ("..." if len(body.message) > 30 else "")
                session.title = title
                db.add(session)
                await db.flush()

            # Notify frontend about the new session
            yield f"data: {json.dumps({'type': 'session_created', 'session': AISessionResponse.model_validate(session).model_dump(mode='json', by_alias=True)}, ensure_ascii=False)}\n\n"

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

            # 额外注入：把最近若干轮的真实查询结果摘要带进上下文，避免 LLM 重复查询
            if session_id:
                prior_msgs = (
                    await db.execute(
                        select(AIMessage)
                        .where(AIMessage.session_id == session_id)
                        .order_by(AIMessage.created_at.desc())
                        .limit(6)
                    )
                ).scalars().all()
                prior_msgs.reverse()
                for pm in prior_msgs:
                    if pm.role == AIMessageRole.assistant and pm.chart_data:
                        charts = pm.chart_data.get("charts") if isinstance(pm.chart_data, dict) else None
                        if charts:
                            summary_parts = [
                                f"- {c.get('chart_type', '?')} 图表：{json.dumps(c.get('option', {}), ensure_ascii=False)[:600]}"
                                for c in charts
                            ]
                            history.append({
                                "role": "system",
                                "content": "【上一轮已生成的图表，请勿重复生成同类型图表】\n" + "\n".join(summary_parts),
                            })

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

                        # 构建 table_ref 和 sample_sql（与 list_datasources 工具输出一致）
                        schema_name = duckdb_client.get_schema_name(str(current_user.id), str(datasource.id), datasource.name)
                        if datasource.source_type in (SourceType.postgresql, SourceType.mysql):
                            meta = datasource.schema_meta if isinstance(datasource.schema_meta, dict) else {}
                            table_name = meta.get("table_name", "data")
                            table_ref = f'"{schema_name}".public."{table_name}"'
                        else:
                            table_ref = f'"{schema_name}"."data"'
                        sample_sql = f"SELECT * FROM {table_ref} LIMIT 1"

                        columns_str = ", ".join(columns) if columns else "（无）"
                        agent_message = (
                            f"【系统注入：当前已连接数据源】\n"
                            f"数据源名称: {datasource.name}\n"
                            f"数据源 ID: {body.datasource_id}\n"
                            f"总行数: {datasource.row_count or '未知'}\n"
                            f"table_ref（FROM 子句必须原样复制）: {table_ref}\n"
                            f"sample_sql（可直接执行看数据结构）: {sample_sql}\n"
                            f"列名（columns，SQL 中必须加双引号）: {columns_str}\n"
                            f"字段详情:\n{field_list}\n\n"
                            f"用户问题: {body.message}"
                        )

            full_content = ""
            collected_charts: list[dict] = []
            seen_chart_keys: set[str] = set()

            # === AI 状态文字过滤器 ===
            # 即使 prompt 禁止，LLM 仍可能输出"正在查询..."、"查询成功"等过程状态。
            # 这里在后端直接过滤，确保不会传到前端。
            _STATUS_EMOJI_RE = re.compile(r'^[📂✅🔍📊📦⚠️]\s')
            _STATUS_KEYWORDS_RE = re.compile(r'正在|已找到|查询成功|查询失败|图表生成|图表已|数据源|浏览|执行')
            _META_START_RE = re.compile(r'^(?:好的|太好了)[！!]')
            _META_KEYWORDS_RE = re.compile(r'查看|连接|分析|生成|拿到|拉取|查询|扫描|数据源|数据已经|数据结构|开始|报告|图表')
            _META_SELF_RE = re.compile(r'^(?:我先|让我)')
            _META_SELF_KEYWORDS_RE = re.compile(r'查询|看看|拉取|获取|扫描|分析一下|预览')

            def _filter_status_text(text: str) -> str:
                """过滤 AI 输出的过程状态文字（"正在查询..."等）。"""
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
                    # 过滤 emoji 状态行（📂 正在浏览...、✅ 查询成功...等）
                    if _STATUS_EMOJI_RE.match(content) and _STATUS_KEYWORDS_RE.search(content):
                        continue
                    # 过滤元话语（"好的！让我先查看..."等）
                    if _META_START_RE.match(content) and _META_KEYWORDS_RE.search(content):
                        continue
                    if _META_SELF_RE.match(content) and _META_SELF_KEYWORDS_RE.search(content):
                        continue
                    filtered.append(line)
                result = '\n'.join(filtered)
                result = re.sub(r'\n{3,}', '\n\n', result)
                return result

            def _chart_key(c: dict) -> str:
                return f"{c.get('chart_type', '?')}::{json.dumps(c.get('option', {}), ensure_ascii=False, sort_keys=True)}"

            # === 代码块过滤状态机 ===
            # LLM 即使被告知不要输出 ```sql/```json，仍可能输出。我们在流式阶段
            # 把这些代码块从可见文本中整块剥离，并保证前后段落分隔。
            _fence_state: str = "closed"  # closed | open
            _fence_buffer: str = ""

            def _strip_and_normalize(delta: str) -> str:
                """剥离 ```...``` 代码块（包括 ```sql 和 ```json），并升级单 \\n 为 \\n\\n。
                返回已经去掉代码块的可显示 delta（不含代码块内任何字符）。"""
                nonlocal _fence_state, _fence_buffer, full_content
                out_parts: list[str] = []

                i = 0
                n = len(delta)
                while i < n:
                    ch = delta[i]

                    if _fence_state == "open":
                        # 进入代码块：累积直到遇到闭合 ```
                        _fence_buffer += ch
                        if ch == "\n":
                            # 保留换行在 buffer，便于判断
                            pass
                        if len(_fence_buffer) >= 3 and _fence_buffer.endswith("```"):
                            # 闭合：丢弃 buffer，强制加段落分隔
                            _fence_state = "closed"
                            _fence_buffer = ""
                            if out_parts and not out_parts[-1].endswith("\n\n"):
                                if out_parts[-1].endswith("\n"):
                                    out_parts[-1] += "\n"
                                else:
                                    out_parts.append("\n\n")
                        i += 1
                        continue

                    # closed：检测开 fence（连续 3 个反引号）
                    if ch == "`":
                        # 尝试往下看是否还有更多反引号（最多 3 个）
                        j = i
                        while j < n and j - i < 3 and delta[j] == "`":
                            j += 1
                        run_len = j - i
                        # 只有正好 3 个反引号才算 fence
                        if run_len == 3:
                            _fence_state = "open"
                            _fence_buffer = ""
                            if out_parts and not out_parts[-1].endswith("\n\n") and not out_parts[-1].endswith("\n"):
                                out_parts.append("\n\n")
                            i = j
                            continue
                        # 不是 fence（单反引号或两个反引号）：作为普通字符输出
                        out_parts.append(delta[i:j])
                        i = j
                        continue

                    # 普通字符
                    out_parts.append(ch)
                    i += 1

                visible_delta = "".join(out_parts)
                # 段落规范化：单 \n 升级为 \n\n
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

            async for event in ai_service.agent_stream(
                user_id=str(current_user.id),
                user_msg=agent_message,
                history=history,
                db_session=db,
                initial_phase="analyzing" if body.datasource_id else "selecting",
            ):
                if event["type"] == "text":
                    raw_delta = event["content"]
                    visible_delta = _strip_and_normalize(raw_delta)
                    visible_delta = _filter_status_text(visible_delta)
                    if visible_delta.strip():
                        yield f"data: {json.dumps({'type': 'message', 'delta': visible_delta}, ensure_ascii=False)}\n\n"
                elif event["type"] == "tool_call":
                    # 不再注入冗长的"正在执行..."旁白，只传事件给前端
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': event['name'], 'args': event.get('args', {})}, ensure_ascii=False)}\n\n"
                elif event["type"] == "tool_result":
                    tname = event.get("name") or ""
                    result_str = event.get("result", "")
                    # 只在查询失败时输出错误提示，成功时静默
                    result_narration = ""
                    try:
                        parsed = json.loads(result_str) if isinstance(result_str, str) else {}
                        if tname == "query_datasource":
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
                            yield f"data: {json.dumps({'type': 'message', 'delta': narrated}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': event['name'], 'result': event['result']}, ensure_ascii=False)}\n\n"
                elif event["type"] == "chart":
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
                    # 不再逐个发送图表事件，done 时一次性批量发送
                elif event["type"] == "done":
                    yield f"data: {json.dumps({'type': 'done', 'charts': collected_charts}, ensure_ascii=False)}\n\n"
                elif event["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': event['message']}, ensure_ascii=False)}\n\n"

            # Save assistant message
            if session_id and (full_content.strip() or collected_charts):
                chart_payload = {"charts": collected_charts} if collected_charts else None
                assistant_msg_model = AIMessage(
                    session_id=session_id,
                    role=AIMessageRole.assistant,
                    content=full_content,
                    chart_data=chart_payload,
                )
                db.add(assistant_msg_model)
                await db.commit()

        except AINotConfiguredError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'AI 未配置'}, ensure_ascii=False)}\n\n"
        except AIUpstreamError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            _log.exception("Agent chat stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': f'服务异常: {str(e)}'}, ensure_ascii=False)}\n\n"

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
                    # 查询前先 DETACH + ATTACH，确保 PostgreSQL 连接有效
                    from app.utils.crypto import decrypt_value, get_encryption_key
                    try:
                        duckdb_client.execute(f'DETACH "{schema_name}"')
                    except Exception:
                        pass
                    conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
                    key = get_encryption_key()
                    if key and conn_info.get("password"):
                        conn_info["password"] = decrypt_value(conn_info["password"], key)
                    conn_info["user"] = conn_info.get("username", "postgres")
                    conn_info["database"] = conn_info.get("db_name", "")
                    from app.connectors.postgres_connector import postgres_connector as pg_conn
                    attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
                    duckdb_client.execute(attach_sql)
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
        # 查询前先 DETACH + ATTACH
        from app.utils.crypto import decrypt_value, get_encryption_key
        try:
            duckdb_client.execute(f'DETACH "{schema_name}"')
        except Exception:
            pass
        conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
        key = get_encryption_key()
        if key and conn_info.get("password"):
            conn_info["password"] = decrypt_value(conn_info["password"], key)
        conn_info["user"] = conn_info.get("username", "postgres")
        conn_info["database"] = conn_info.get("db_name", "")
        from app.connectors.postgres_connector import postgres_connector as pg_conn
        attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
        duckdb_client.execute(attach_sql)
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
            # Ensure fresh connection
            from app.utils.crypto import decrypt_value, get_encryption_key

            try:
                duckdb_client.execute(f'DETACH "{schema_name}"')
            except Exception:
                pass
            conn_info = dict(datasource.connection_config) if datasource.connection_config else {}
            key = get_encryption_key()
            if key and conn_info.get("password"):
                conn_info["password"] = decrypt_value(conn_info["password"], key)
            conn_info["user"] = conn_info.get("username", "postgres")
            conn_info["database"] = conn_info.get("db_name", "")
            from app.connectors.postgres_connector import postgres_connector as pg_conn

            attach_sql = pg_conn.get_attach_sql(conn_info, schema_name)
            duckdb_client.execute(attach_sql)
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
                    canvas_ctx += f"- {b.get('title', '图表')} (类型: {b.get('chartType', '?')}, 维度: {b.get('dimensions', [])}, 度量: {b.get('measures', [])})\n"
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
        # 即使没有选中图表块，也告知 AI 可以根据字段自由推荐图表
        if not canvas_ctx.strip():
            canvas_ctx = "当前画布为空，你可以根据数据源字段自由推荐图表配置。\n"

    # 组装 user_msg：表引用 + 字段信息 + 画布上下文，交给 agent_stream 统一编排
    canvas_tools_note = (
        "\n（你可以在画布上直接搭报告：用 add_text_block 写标题/叙事，"
        "add_chart_block 建图，update_chart_block/remove_block 改删已有块。）"
    )
    if datasource:
        user_msg_parts = [f"已连接数据源：{datasource.name}（FROM 用表引用：{table_ref}）"]
    else:
        user_msg_parts = ["当前未连接数据源，你可以与用户就其需求自由对话。"]
    if field_info:
        user_msg_parts.append(field_info)
    if canvas_ctx:
        user_msg_parts.append(canvas_ctx.strip())
    user_msg_parts.append(f"用户问题：{body.message}")
    user_msg = "\n".join(user_msg_parts) + canvas_tools_note

    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    async def event_generator():
        from app.schemas import AISessionResponse

        ai_service = AIService(LLMClient(settings))
        full_content = ""
        session_id: uuid.UUID | None = None
        try:
            # ---- 1. 解析 / 创建会话 ----
            if body.session_id:
                try:
                    sid_uuid = uuid.UUID(body.session_id)
                except (ValueError, TypeError):
                    sid_uuid = None
                if sid_uuid:
                    sess = (
                        await db.execute(
                            select(AISession).where(
                                AISession.id == sid_uuid,
                                AISession.user_id == current_user.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if sess:
                        session_id = sess.id

            if session_id is None and body.message.strip():
                sess = AISession(
                    user_id=current_user.id,
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

            # ---- 2. 保存用户消息 ----
            if session_id:
                user_msg_model = AIMessage(
                    session_id=session_id,
                    role=AIMessageRole.user,
                    content=body.message,
                )
                db.add(user_msg_model)
                await db.flush()
                # Auto-title
                if session_id:
                    sess_result = await db.execute(
                        select(AISession).where(AISession.id == session_id)
                    )
                    sess = sess_result.scalar_one_or_none()
                    if sess and sess.title in (None, "新对话", "画布对话"):
                        title = body.message[:30] + ("..." if len(body.message) > 30 else "")
                        sess.title = title
                        db.add(sess)
                        await db.flush()

            # ---- 3. 加载历史（最近 6 轮） ----
            history: list[dict] = []
            if session_id:
                prior_msgs = (
                    await db.execute(
                        select(AIMessage)
                        .where(AIMessage.session_id == session_id)
                        .order_by(AIMessage.created_at.desc())
                        .limit(12)
                    )
                ).scalars().all()
                prior_msgs.reverse()
                for pm in prior_msgs:
                    if pm.role == AIMessageRole.assistant:
                        # 跳过刚保存的当前 user_msg 后的 assistant 消息（尚未生成）
                        continue
                    history.append({"role": pm.role.value, "content": pm.content})

            # ---- 4. Agent 流式执行 ----
            async for event in ai_service.agent_stream(
                user_id=str(current_user.id),
                user_msg=user_msg,
                history=history,
                db_session=db,
                initial_phase="selecting",
                system_prompt_override=CANVAS_AGENT_SYSTEM,
            ):
                ev_type = event.get("type")
                if ev_type == "text":
                    delta = event.get("content", "")
                    full_content += delta
                    yield _sse({"type": "message", "delta": delta})
                elif ev_type == "tool_call":
                    yield _sse({"type": "tool_call", "name": event.get("name", ""), "args": event.get("args", {})})
                elif ev_type == "tool_result":
                    name = event.get("name", "")
                    result_raw = event.get("result", "")
                    yield _sse({"type": "tool_result", "name": name, "result": result_raw})
                    try:
                        r = json.loads(result_raw) if isinstance(result_raw, str) else {}
                        action = r.get("canvas_action") if isinstance(r, dict) else None
                        if isinstance(action, dict):
                            yield _sse({"type": "canvas_action", **action})
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif ev_type == "chart":
                    chart_type = event.get("chart_type")
                    option = event.get("option")
                    if option is not None:
                        yield _sse({"type": "chart", "chart_type": chart_type, "option": option})
                elif ev_type == "status":
                    yield _sse({"type": "step", "title": event.get("message", ""), "status": "running"})
                elif ev_type == "error":
                    yield _sse({"type": "error", "message": event.get("message", "服务异常")})
                elif ev_type == "done":
                    yield _sse({"type": "done"})

            # ---- 5. 保存助手消息 ----
            if session_id and full_content.strip():
                assistant_msg = AIMessage(
                    session_id=session_id,
                    role=AIMessageRole.assistant,
                    content=full_content,
                )
                db.add(assistant_msg)
                await db.commit()

        except AINotConfiguredError:
            yield _sse({"type": "error", "message": "AI 未配置"})
        except AIUpstreamError as e:
            yield _sse({"type": "error", "message": str(e)})
        except Exception as e:
            _log.exception("Canvas AI chat error")
            yield _sse({"type": "error", "message": f"服务异常: {str(e)}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )