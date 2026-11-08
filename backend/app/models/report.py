import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ReportStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    shared = "shared"
    deleted = "deleted"


class ReportSourceType(str, enum.Enum):
    canvas = "canvas"
    dashboard = "dashboard"
    manual = "manual"
    ai_insight = "ai_insight"


class Report(TimestampMixin, Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[ReportSourceType] = mapped_column(
        Enum(ReportSourceType, name="report_source_type"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    snapshot_blocks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status"), default=ReportStatus.draft, nullable=False
    )
    share_token: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    # 分享链接过期时间：NULL = 永久有效；撤销分享时与 share_token 一起清空。
    # 由 0023_share_link_controls 迁移新增（报表的"软删"是 status=deleted，没有 deleted_at 列）。
    share_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("User", back_populates="reports")
