import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AIMemory(Base):
    """会话级压缩记忆：每轮结束时把较早的历史（工具结果等）压缩成摘要持久化，
    下一轮开始时重新注入上下文，实现跨轮记忆回流。

    每个会话只有一行（upsert 语义），保存最近一次压缩出的摘要。
    """

    __tablename__ = "ai_memories"
    __table_args__ = (UniqueConstraint("session_id", name="uq_ai_memories_session"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    covered_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 记忆合并水位：已并入长期记忆的最后一条消息 id。
    # 刻意不加外键——canvas 的"新对话"会 delete 该会话全部消息而保留记忆行，
    # 加了外键会连带删记忆；读取端对"水位查不到"做兜底（退回从头取最早未并入段）。
    last_merged_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    merge_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    merge_fail_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )
