import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MetricUsage(Base):
    """指标使用追踪：记录指标在 Dashboard / Canvas / AI Query 中的引用。

    - usage_type: 'dashboard' / 'canvas' / 'ai_query'
    - usage_ref_id: 引用方资源的 ID（UUID 字符串），可空（AI 临时查询可能不持久化）
    - user_id: 触发引用的用户，可空（系统级引用）
    - deleted_at: 软删除，引用方删除/解绑时设置，查询默认过滤
    """

    __tablename__ = "metric_usage"
    __table_args__ = (
        Index("ix_metric_usage_metric_type", "metric_id", "usage_type"),
        Index("ix_metric_usage_type_ref", "usage_type", "usage_ref_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("metric_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    usage_type: Mapped[str] = mapped_column(String(32), nullable=False)
    usage_ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    metric = relationship("MetricDefinition")
    user = relationship("User")
