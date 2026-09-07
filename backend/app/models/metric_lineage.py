import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MetricLineage(Base):
    """指标血缘：记录指标定义到源字段的派生关系。

    一行表示一条"源字段 -> 目标字段"的转换，辅助理解指标口径。
    - source_field: 上游表中的原始列名（如 amount / order_id）
    - target_field: 指标中暴露的字段名（默认与 source_field 相同）
    - transform: 转换描述（如 SUM / COUNT_DISTINCT / DIVIDE），可空
    - dataset_id: 数据源 ID，可空（兼容 user_id=None 的全局模板）
    """

    __tablename__ = "metric_lineage"
    __table_args__ = (
        Index("ix_metric_lineage_metric_source", "metric_id", "source_field"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("metric_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_field: Mapped[str] = mapped_column(String(200), nullable=False)
    transform: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    target_field: Mapped[str] = mapped_column(String(200), nullable=False)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasources.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    metric = relationship("MetricDefinition")
    datasource = relationship("DataSource")
