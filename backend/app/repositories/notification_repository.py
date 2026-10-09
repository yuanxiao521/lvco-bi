"""Notification Repository。

封装 Notification 实体的数据访问逻辑。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification

logger = logging.getLogger(__name__)


class SQLAlchemyNotificationRepository:
    """Notification 实体的 SQLAlchemy Repository 实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id: uuid.UUID,
        notif_type: str,
        title: str,
        body: str,
        *,
        link_url: str | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> Notification:
        """创建通知。"""
        notif = Notification(
            user_id=user_id,
            type=notif_type,
            title=title,
            body=body,
            link_url=link_url,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_=metadata,
        )
        self.db.add(notif)
        await self.db.flush()
        await self.db.refresh(notif)
        logger.info(f"notification_created user_id={user_id} type={notif_type}")
        return notif

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
        unread_only: bool = False,
    ) -> tuple[list[Notification], int]:
        """分页查询用户通知。"""
        conditions = [Notification.user_id == user_id]
        if unread_only:
            conditions.append(Notification.read.is_(False))

        # 总数
        count_q = (
            select(func.count())
            .select_from(Notification)
            .where(*conditions)
        )
        total = (await self.db.execute(count_q)).scalar() or 0

        # 列表
        offset = (page - 1) * page_size
        q = (
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list((await self.db.execute(q)).scalars().all())
        return items, total

    async def count_unread(self, user_id: uuid.UUID) -> int:
        """统计未读数量。"""
        result = await self.db.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user_id, Notification.read.is_(False))
        )
        return result.scalar() or 0

    async def mark_read(self, user_id: uuid.UUID, notif_id: uuid.UUID) -> bool:
        """标记单条已读。"""
        result = await self.db.execute(
            update(Notification)
            .where(
                Notification.id == notif_id,
                Notification.user_id == user_id,
                Notification.read.is_(False),
            )
            .values(read=True, read_at=datetime.now(timezone.utc))
        )
        await self.db.flush()
        return (result.rowcount or 0) > 0

    async def mark_all_read(self, user_id: uuid.UUID) -> int:
        """全部标记已读，返回更新条数。"""
        result = await self.db.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read.is_(False))
            .values(read=True, read_at=datetime.now(timezone.utc))
        )
        await self.db.flush()
        return result.rowcount or 0

    async def clear_all(self, user_id: uuid.UUID) -> int:
        """物理删除用户所有通知。"""
        result = await self.db.execute(
            delete(Notification).where(Notification.user_id == user_id)
        )
        await self.db.flush()
        return result.rowcount or 0

    async def create_bulk(
        self,
        notifications: list[dict[str, Any]],
    ) -> int:
        """批量创建通知。"""
        for n in notifications:
            notif = Notification(
                user_id=n["user_id"],
                type=n["type"],
                title=n["title"],
                body=n["body"],
                link_url=n.get("link_url"),
                resource_type=n.get("resource_type"),
                resource_id=n.get("resource_id"),
                metadata_=n.get("metadata"),
            )
            self.db.add(notif)
        await self.db.flush()
        return len(notifications)
