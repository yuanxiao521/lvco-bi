"""用户偏好仓库实现

基于 SQLAlchemy 实现 UserPreferenceRepository 协议
"""
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_preference import UserPreference
from app.repositories.protocols import UserPreferenceRepository

logger = logging.getLogger(__name__)


class SQLAlchemyUserPreferenceRepository(UserPreferenceRepository):
    """基于 SQLAlchemy 的用户偏好仓库实现"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
    ) -> UserPreference | None:
        """获取单个偏好"""
        logger.debug(
            f"get_preference: user_id={user_id}, type={preference_type}, key={preference_key}"
        )

        stmt = select(UserPreference).where(
            and_(
                UserPreference.user_id == user_id,
                UserPreference.preference_type == preference_type,
                UserPreference.preference_key == preference_key,
            )
        )

        result = await self.db.execute(stmt)
        preference = result.scalar_one_or_none()

        if preference:
            logger.debug(
                f"get_preference: found, strength={preference.strength:.2f}"
            )
        else:
            logger.debug("get_preference: not found")

        return preference

    async def get_user_preferences(
        self,
        user_id: UUID,
        preference_type: str | None = None,
    ) -> list[UserPreference]:
        """获取用户的所有偏好（可选按类型过滤）"""
        logger.debug(
            f"get_user_preferences: user_id={user_id}, type={preference_type}"
        )

        stmt = select(UserPreference).where(UserPreference.user_id == user_id)

        if preference_type:
            stmt = stmt.where(UserPreference.preference_type == preference_type)

        stmt = stmt.order_by(UserPreference.strength.desc())

        result = await self.db.execute(stmt)
        preferences = list(result.scalars().all())

        logger.debug(f"get_user_preferences: found {len(preferences)} preferences")

        return preferences

    async def get_top_preferences(
        self,
        user_id: UUID,
        preference_type: str,
        limit: int = 3,
    ) -> list[UserPreference]:
        """获取某类型的前 N 个最强偏好"""
        logger.debug(
            f"get_top_preferences: user_id={user_id}, type={preference_type}, limit={limit}"
        )

        stmt = (
            select(UserPreference)
            .where(
                and_(
                    UserPreference.user_id == user_id,
                    UserPreference.preference_type == preference_type,
                )
            )
            .order_by(UserPreference.strength.desc())
            .limit(limit)
        )

        result = await self.db.execute(stmt)
        preferences = list(result.scalars().all())

        logger.debug(f"get_top_preferences: found {len(preferences)} preferences")

        return preferences

    async def create_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
        preference_value: dict,
        strength: float,
        evidence_count: int,
    ) -> UserPreference:
        """创建新偏好"""
        logger.debug(
            f"create_preference: user_id={user_id}, type={preference_type}, "
            f"key={preference_key}, strength={strength:.2f}"
        )

        preference = UserPreference(
            user_id=user_id,
            preference_type=preference_type,
            preference_key=preference_key,
            preference_value=preference_value,
            strength=strength,
            evidence_count=evidence_count,
            last_used_at=datetime.utcnow(),
        )
        self.db.add(preference)
        await self.db.flush()

        logger.debug(
            f"create_preference: created, id={preference.id}, strength={preference.strength:.2f}"
        )

        return preference

    async def update_preference(
        self,
        preference: UserPreference,
        preference_value: dict,
        strength: float,
        evidence_count: int,
    ) -> UserPreference:
        """更新偏好"""
        logger.debug(
            f"update_preference: id={preference.id}, strength={strength:.2f}, "
            f"evidence={evidence_count}"
        )

        preference.preference_value = preference_value
        preference.strength = strength
        preference.evidence_count = evidence_count
        preference.last_used_at = datetime.utcnow()

        await self.db.flush()

        logger.debug(
            f"update_preference: updated, strength={preference.strength:.2f}"
        )

        return preference

    async def delete_preference(self, preference: UserPreference) -> None:
        """删除偏好"""
        logger.debug(f"delete_preference: id={preference.id}")

        self.db.delete(preference)
        await self.db.flush()

        logger.debug("delete_preference: deleted")
