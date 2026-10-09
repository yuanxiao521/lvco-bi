"""NotificationService - 写入 Notification 表 + SSE 实时推送

重构后使用 NotificationRepository 访问数据，自身只负责：
1. 业务编排（写 DB + 推 SSE）
2. SSE 失败的优雅降级
3. 兼容 str/UUID 混合入参

设计要点:
- SSE 推送失败仅 log，不阻断 DB 写入
- 调度器/InsightRunner 调用 push() 时传入独立 session（避免事务嵌套）
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.core.sse import sse_manager
from app.models.notification import Notification, NotificationType
from app.repositories.notification_repository import SQLAlchemyNotificationRepository

log = structlog.get_logger("notification_service")


class NotificationService:
    """通知服务 - DB 持久化 + SSE 推送"""

    def __init__(self, notif_repo: SQLAlchemyNotificationRepository) -> None:
        self.notif_repo = notif_repo

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

        notif = await self.notif_repo.create(
            user_id=user_id,
            notif_type=type_,
            title=title[:200],
            body=body,
            link_url=link_url,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )

        # SSE 推送（失败不影响已写入的通知）
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
        """分页查询用户通知。"""
        return await self.notif_repo.list_for_user(
            user_id=user_id, page=page, page_size=page_size, unread_only=unread_only
        )

    async def unread_count(self, user_id: uuid.UUID) -> int:
        """未读数。"""
        return await self.notif_repo.count_unread(user_id)

    async def mark_read(self, notif_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """标记单条已读。"""
        return await self.notif_repo.mark_read(user_id, notif_id)

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """全部已读。"""
        return await self.notif_repo.mark_all_read(user_id)

    async def clear_all(self, user_id: uuid.UUID) -> int:
        """清空所有通知。"""
        return await self.notif_repo.clear_all(user_id)


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

    内部创建独立 session，提交后关闭。
    """
    from app.core.database import async_session_factory

    async with async_session_factory() as db:
        repo = SQLAlchemyNotificationRepository(db)
        service = NotificationService(repo)
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
