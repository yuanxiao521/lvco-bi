import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MetricVersion(Base):
    """指标版本快照：每次指标口径变更保存一条完整定义快照。

    - snapshot: 完整定义 JSON（key / name / formula / agg_kind / formula_type / depends_on ...）
    - change_note: 变更说明（人工填写或系统摘要）
    - 唯一约束 (metric_id, version)：同一指标的版本号不可重复
    """

    __tablename__ = "metric_versions"
    __table_args__ = (
        UniqueConstraint("metric_id", "version", name="uq_metric_versions_metric_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("metric_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    metric = relationship("MetricDefinition")
    creator = relationship("User")
