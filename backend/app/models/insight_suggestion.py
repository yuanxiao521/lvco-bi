import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SuggestionStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    dismissed = "dismissed"


class InsightSuggestion(Base):
    __tablename__ = "insight_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    datasource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False
    )

    table_name: Mapped[str] = mapped_column(String(200), nullable=False)
    time_field: Mapped[str | None] = mapped_column(String(200), nullable=True)
    measure_fields: Mapped[list[str] | None] = mapped_column(ARRAY(String(200)), nullable=True)
    dimension_fields: Mapped[list[str] | None] = mapped_column(ARRAY(String(200)), nullable=True)

    suggested_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    suggested_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    row_count_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    update_frequency: Mapped[str | None] = mapped_column(String(20), nullable=True)

    status: Mapped[SuggestionStatus] = mapped_column(
        Enum(SuggestionStatus, name="suggestion_status"),
        nullable=False,
        default=SuggestionStatus.pending,
    )
    accepted_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("insight_rules.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
