import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AISession(Base):
    __tablename__ = "ai_sessions"
    # 入口隔离索引：画布按 (entry='canvas', canvas_id) 查会话
    __table_args__ = (
        Index("idx_ai_sessions_canvas", "entry", "canvas_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 会话入口：chat（对话助手）/ canvas（画布助手），旧会话默认 chat
    entry: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="chat", default="chat"
    )
    # 归属画布（entry='canvas' 时有值；新画布草稿可为空）
    canvas_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    model: Mapped[str] = mapped_column(default="gpt-4o", nullable=False)
    title: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user = relationship("User", back_populates="ai_sessions")
    messages = relationship(
        "AIMessage", back_populates="session", cascade="all, delete-orphan"
    )
