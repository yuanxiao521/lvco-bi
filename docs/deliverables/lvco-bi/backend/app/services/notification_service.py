"""NotificationService - 写入 Notification 表 + SSE 实时推送

职责:
1. push(): 写 DB + 推 SSE（原子，SSE 失败不影响 DB）
2. list(): 分页查询用户通知
3. unread_count(): 未读数
4. mark_read/mark_all_read/clear(): 状态更新

设计要点:
- SSE 推送失败仅 log，不阻断 DB 写入
- 调度器/InsightRunner 调用 push() 时传入独立 session（避免事务嵌套）
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sse import sse_manager
from app.models.notification import Notification, NotificationType

log = structlog.get_logger("notification_service")


class NotificationService:
    """通知服务 - DB 持久化 + SSE 推送"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def push(
        self,
        user_id: uuid.UUID | str,
        type_: NotificationType | str,
        *,
        title: str,
        body: str,
        link_url: str | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Notification:
        """写入通知 + 推送 SSE

        SSE 失败不阻断 DB 写入（仅 log warning）
        """
        if isinstance(user_id, str):
            user_id = uuid.UUID(user_id)
        if isinstance(type_, str):
            type_ = NotificationType(type_)
        if isinstance(resource_id, str):
            resource_id = uuid.UUID(resource_id)

        notif = Notification(
            user_id=user_id,
            type=type_,
            title=title[:200],
            body=body,
            link_url=link_url,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_=metadata,
        )
        self.db.add(notif)
        await self.db.commit()
        await self.db.refresh(notif)

        # SSE 推送（失败不影响已 commit 的通知）
        try:
            sse_payload = {
                "id": str(notif.id),
                "type": notif.type.value if notif.type else None,
                "title": notif.title,
                "body": notif.body,
                "linkUrl": notif.link_url,
                "resourceType": notif.resource_type,
                "resourceId": str(notif.resource_id) if notif.resource_id else None,
                "createdAt": notif.created_at.isoformat() if notif.created_at else None,
            }
            await sse_manager.publish(user_id, "notification", sse_payload)
        except Exception as e:
            log.warning("sse_push_failed", user_id=str(user_id), error=str(e))

        log.info(
            "notification_pushed",
            user_id=str(user_id),
            notif_id=str(notif.id),
            type=type_.value if hasattr(type_, "value") else str(type_),
        )
        return notif

    async def list(
        self,
        user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
        unread_only: bool = False,
    ) -> tuple[list[Notification], int]:
        """分页查询用户通知"""
        q = select(Notification).where(Notification.user_id == user_id)
        count_q = select(func.count()).select_from(Notification).where(Notification.user_id == user_id)

        if unread_only:
            q = q.where(Notification.read.is_(False))
            count_q = count_q.where(Notification.read.is_(False))

        total = (await self.db.execute(count_q)).scalar() or 0
        q = (
            q.order_by(Notification.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.db.execute(q)).scalars().all())
        return items, total

    async def unread_count(self, user_id: uuid.UUID) -> int:
        """未读数"""
        result = await self.db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read.is_(False))
        )
        return result.scalar() or 0

    async def mark_read(self, notif_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """标记单条已读"""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notif_id,
                Notification.user_id == user_id,
                Notification.read.is_(False),
            )
            .values(read=True, read_at=datetime.utcnow())
        )
        await self.db.commit()
        return (result.rowcount or 0) > 0

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """全部已读，返回更新条数"""
        result = await self.db.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read.is_(False))
            .values(read=True, read_at=datetime.utcnow())
        )
        await self.db.commit()
        return result.rowcount or 0

    async def clear_all(self, user_id: uuid.UUID) -> int:
        """清空用户所有通知（物理删除），返回删除条数"""
        result = await self.db.execute(
            delete(Notification).where(Notification.user_id == user_id)
        )
        await self.db.commit()
        return result.rowcount or 0


# 单例 helper（用于无 session 上下文，如 scheduler 调度入口）
async def push_notification(
    user_id: uuid.UUID | str,
    type_: NotificationType | str,
    *,
    title: str,
    body: str,
    link_url: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Notification:
    """独立 session 版本的 push（用于 scheduler / runner 等无 db 上下文场景）

    内部创建独立 session，提交后关闭
    """
    from app.core.database import async_session_factory

    async with async_session_factory() as db:
        service = NotificationService(db)
        return await service.push(
            user_id,
            type_,
            title=title,
            body=body,
            link_url=link_url,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )
