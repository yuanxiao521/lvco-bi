import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SourceType(str, enum.Enum):
    csv = "csv"
    excel = "excel"
    mysql = "mysql"
    postgresql = "postgresql"


class DatasourceStatus(str, enum.Enum):
    connected = "connected"
    disconnected = "disconnected"
    syncing = "syncing"


class DataSource(TimestampMixin, Base):
    __tablename__ = "datasources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type"), nullable=False
    )
    connection_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    schema_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[DatasourceStatus] = mapped_column(
        Enum(DatasourceStatus, name="datasource_status"),
        default=DatasourceStatus.disconnected,
        nullable=False,
        index=True,
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user = relationship("User", back_populates="datasources")
    canvases = relationship("Canvas", back_populates="datasource")
