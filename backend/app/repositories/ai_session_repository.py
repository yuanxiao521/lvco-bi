"""AI Session & Message Repository。

封装 AISession 和 AIMessage 实体的数据访问逻辑。
两个 Repository 共享同一个 AsyncSession，保证事务一致性。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_message import AIMessage, AIMessageRole
from app.models.ai_session import AISession

logger = logging.getLogger(__name__)


class SQLAlchemyAISessionRepository:
    """AISession 实体的 SQLAlchemy Repository 实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, session_id: uuid.UUID, user_id: uuid.UUID) -> AISession | None:
        """根据 ID + 用户 ID 查询会话（防止越权访问）。"""
        result = await self.db.execute(
            select(AISession).where(
                AISession.id == session_id, AISession.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AISession], int]:
        """分页查询用户会话列表。"""
        count_q = (
            select(func.count()).select_from(AISession).where(AISession.user_id == user_id)
        )
        total = (await self.db.execute(count_q)).scalar() or 0

        q = (
            select(AISession)
            .where(AISession.user_id == user_id)
            .order_by(AISession.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.db.execute(q)).scalars().all())
        return items, total

    async def create(
        self,
        user_id: uuid.UUID,
        model: str = "gpt-4o",
        title: str | None = None,
    ) -> AISession:
        """创建新会话。"""
        session = AISession(user_id=user_id, model=model, title=title)
        self.db.add(session)
        await self.db.flush()
        await self.db.refresh(session)
        logger.info(f"ai_session_created user_id={user_id} session_id={session.id}")
        return session

    async def update_title(self, session_id: uuid.UUID, title: str) -> AISession | None:
        """更新会话标题。"""
        result = await self.db.execute(
            select(AISession).where(AISession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session is None:
            return None
        session.title = title
        await self.db.flush()
        await self.db.refresh(session)
        return session

    async def delete(self, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """删除会话（级联删除 messages）。"""
        session = await self.get_by_id(session_id, user_id)
        if session is None:
            return False
        self.db.delete(session)
        await self.db.flush()
        logger.info(f"ai_session_deleted user_id={user_id} session_id={session_id}")
        return True


class SQLAlchemyAIMessageRepository:
    """AIMessage 实体的 SQLAlchemy Repository 实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_for_session(self, session_id: uuid.UUID) -> list[AIMessage]:
        """查询会话的所有消息，按时间正序。"""
        result = await self.db.execute(
            select(AIMessage)
            .where(AIMessage.session_id == session_id)
            .order_by(AIMessage.created_at.asc())
        )
        return list(result.scalars().all())

    async def create(
        self,
        session_id: uuid.UUID,
        role: AIMessageRole | str,
        content: str,
        chart_data: dict | None = None,
    ) -> AIMessage:
        """创建一条消息。"""
        if isinstance(role, str):
            role = AIMessageRole(role)
        msg = AIMessage(
            session_id=session_id,
            role=role,
            content=content,
            chart_data=chart_data,
        )
        self.db.add(msg)
        await self.db.flush()
        await self.db.refresh(msg)
        return msg

    async def delete(self, message_id: uuid.UUID) -> bool:
        """删除消息。"""
        result = await self.db.execute(
            select(AIMessage).where(AIMessage.id == message_id)
        )
        msg = result.scalar_one_or_none()
        if msg is None:
            return False
        self.db.delete(msg)
        await self.db.flush()
        return True
