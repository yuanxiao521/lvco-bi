"""User Repository。

封装 User 实体的数据访问逻辑，遵循 Repository 模式：
- 只通过 SQLAlchemy 操作数据库
- 方法返回 ORM 对象（由 Service 层转换为 DTO）
- 只调用 flush()/refresh()，不调用 commit()（事务在 Service/UnitOfWork 层控制）
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

logger = logging.getLogger(__name__)


class SQLAlchemyUserRepository:
    """User 实体的 SQLAlchemy Repository 实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, user_id: UUID) -> User | None:
        """根据 ID 查询用户。"""
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        """根据邮箱查询用户。"""
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def create(
        self,
        email: str,
        password_hash: str,
        display_name: str,
        role: str | None = None,
        avatar_url: str | None = None,
    ) -> User:
        """创建新用户。

        Args:
            email: 邮箱
            password_hash: 已加密的密码哈希
            display_name: 显示名称
            role: 角色（可选，默认为 None）
            avatar_url: 头像 URL（可选）

        Returns:
            已创建且刷新的 User 对象
        """
        user = User(
            email=email,
            password_hash=password_hash,
            display_name=display_name,
            role=role,
            avatar_url=avatar_url,
        )
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        logger.info(f"user_created user_id={user.id} email={email}")
        return user

    async def update(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        avatar_url: str | None = None,
        password_hash: str | None = None,
    ) -> User | None:
        """更新用户资料。

        Args:
            user_id: 用户 ID
            display_name: 新显示名（None 表示不修改）
            avatar_url: 新头像 URL（None 表示不修改）
            password_hash: 新密码哈希（None 表示不修改）

        Returns:
            更新后的 User，找不到则返回 None
        """
        values: dict[str, Any] = {}
        if display_name is not None:
            values["display_name"] = display_name
        if avatar_url is not None:
            values["avatar_url"] = avatar_url
        if password_hash is not None:
            values["password_hash"] = password_hash
        if not values:
            return await self.get_by_id(user_id)

        result = await self.db.execute(
            update(User).where(User.id == user_id).values(**values).returning(User)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            await self.db.refresh(user)
            logger.info(f"user_updated user_id={user_id}")
        return user

    async def delete(self, user_id: UUID) -> bool:
        """物理删除用户（一般用软删除，业务上少用）。"""
        user = await self.get_by_id(user_id)
        if user is None:
            return False
        self.db.delete(user)
        await self.db.flush()
        logger.info(f"user_deleted user_id={user_id}")
        return True
