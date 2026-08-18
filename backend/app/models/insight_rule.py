import enum
import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, Time
from sqlalchemy.dialects.postgresql import ARRAY, JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ReportType(str, enum.Enum):
    daily_report = "daily_report"
    weekly_report = "weekly_report"


class ScheduleType(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


class InsightRule(TimestampMixin, Base):
    __tablename__ = "insight_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    datasource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    query_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    detect_types: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)), nullable=False, default=lambda: ["anomaly", "trend", "ratio"]
    )
    threshold: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    report_type: Mapped[ReportType] = mapped_column(
        Enum(ReportType, name="report_type"), nullable=False, default=ReportType.daily_report
    )
    schedule: Mapped[ScheduleType] = mapped_column(
        Enum(ScheduleType, name="schedule_type"), nullable=False, default=ScheduleType.daily
    )
    schedule_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(9, 0, 0))

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[RunStatus | None] = mapped_column(
        Enum(RunStatus, name="run_status"), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    records = relationship("InsightRecord", back_populates="rule", cascade="all, delete-orphan")
