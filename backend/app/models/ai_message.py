import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class AIMessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[AIMessageRole] = mapped_column(
        Enum(AIMessageRole, name="ai_message_role"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chart_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 本轮过程步骤投影（turn 收尾时从 agent_calls 事实源生成，随消息永久）。
    # 形状与前端 ActivityFeed 的 FeedStep 对齐：[{id,kind,toolName,ok,durationMs,summary,tokens?}]
    # 事实源仍是 agent_calls；这里是 UI 物化视图——历史消息展开零查询成本。
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session = relationship("AISession", back_populates="messages")
