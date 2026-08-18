"""通知中心 API - 列表/未读数/已读/清空/SSE 流"""
import asyncio
import math
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.sse import sse_manager
from app.models.user import User
from app.schemas import SuccessResponse
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["通知中心"])


def _serialize(n) -> dict:
    return {
        "id": str(n.id),
        "type": n.type.value if n.type else None,
        "title": n.title,
        "body": n.body,
        "linkUrl": n.link_url,
        "resourceType": n.resource_type,
        "resourceId": str(n.resource_id) if n.resource_id else None,
        "metadata": n.metadata_,
        "read": n.read,
        "readAt": n.read_at.isoformat() if n.read_at else None,
        "createdAt": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("")
async def list_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = NotificationService(db)
    items, total = await service.list(current_user.id, page, page_size, unread_only)
    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(
        data={
            "items": [_serialize(n) for n in items],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "pages": pages,
            "unreadCount": await service.unread_count(current_user.id),
        }
    )


@router.get("/unread_count")
async def unread_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = NotificationService(db)
    count = await service.unread_count(current_user.id)
    return SuccessResponse(data={"unreadCount": count})


@router.post("/{notif_id}/read")
async def mark_read(
    notif_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = NotificationService(db)
    updated = await service.mark_read(notif_id, current_user.id)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "通知不存在或已读"},
        )
    return SuccessResponse(data={"read": True})


@router.post("/read_all")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = NotificationService(db)
    count = await service.mark_all_read(current_user.id)
    return SuccessResponse(data={"updated": count})


@router.delete("")
async def clear_all(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = NotificationService(db)
    count = await service.clear_all(current_user.id)
    return SuccessResponse(data={"deleted": count})


@router.get("/stream")
async def notification_stream(
    token: str | None = Query(None),
    current_user: User = Depends(get_current_user),
):
    """SSE 端点 - 实时推送通知事件

    支持两种认证方式:
    1. Authorization: Bearer <token>（标准方式）
    2. ?token=<token>（EventSource 专用，因 EventSource 不支持自定义 header）

    客户端用 EventSource 订阅:
    ```js
    const es = new EventSource('/api/v1/notifications/stream?token=...');
    es.addEventListener('notification', (e) => {
      const data = JSON.parse(e.data);
      // 处理通知
    });
    ```
    """
    user_id = current_user.id
    queue = await sse_manager.subscribe(user_id)

    async def event_generator():
        try:
            # 发送初始连接确认
            yield sse_manager.format_event("connected", {"userId": str(user_id), "ts": datetime.utcnow().isoformat()})

            while True:
                try:
                    # 等 30 秒无事件则发心跳（保活，防代理超时）
                    event, data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield sse_manager.format_event(event, data)
                except asyncio.TimeoutError:
                    yield sse_manager.format_event("ping", {"ts": datetime.utcnow().isoformat()})
        except asyncio.CancelledError:
            pass
        finally:
            await sse_manager.unsubscribe(user_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 不缓冲
        },
    )
