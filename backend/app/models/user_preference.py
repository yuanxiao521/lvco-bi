"""用户偏好记忆模型

存储用户在 Agent 交互过程中的偏好，支持：
- 显式偏好：用户明确表达（如"我喜欢折线图"）
- 隐式偏好：从用户行为推断（如连续 3 次选择折线图）
- 偏好强度衰减：长时间未使用的偏好自动衰减
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class UserPreference(Base):
    """用户偏好记忆表"""
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    
    # 偏好类型：chart_type, color_scheme, dimension, aggregation, analysis_focus
    preference_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    
    # 偏好键：如 line, bar, blue, time
    preference_key: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    
    # 偏好值（JSON 格式，支持复杂偏好配置）
    preference_value: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    
    # 偏好强度：0-1，显式表达=1.0，隐式推断=0.3-0.7
    strength: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.5
    )
    
    # 证据数量：用户表达该偏好的次数
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    
    # 最后使用时间（用于衰减过期偏好）
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now()
    )

    user = relationship("User", back_populates="preferences")

    def __repr__(self) -> str:
        return (
            f"<UserPreference(id={self.id}, user_id={self.user_id}, "
            f"type={self.preference_type}, key={self.preference_key}, "
            f"strength={self.strength:.2f}, evidence={self.evidence_count})>"
        )
