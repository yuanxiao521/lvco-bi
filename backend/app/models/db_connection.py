import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ConnectionHealth(str, enum.Enum):
    """连接级健康状态（探测结果，不是业务状态）。"""

    ok = "ok"
    failed = "failed"


class DBConnection(TimestampMixin, Base):
    """一个物理数据库连接（凭据只在这里存一份）。

    为什么单独建表：改造前"连一张表 = 一个 datasource 行 = 一份密码"，选 3 张表就有
    3 份密文副本，改一次密码要改 3 次、轮换没有原子性。现在：

        db_connections（1 份凭据 + 主机/TLS 配置）
            └── datasources.connection_id（N 张表，各自保留 table_name 与字段语义）

    这样"轮换密码"是改一行、影响该连接下所有表；"健康检查"也是连接级一次探测。
    """

    __tablename__ = "db_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(
        ENUM("csv", "excel", "mysql", "postgresql", name="source_type", create_type=False),
        nullable=False,
    )
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    db_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 只有密文：enc:v2:<b64(nonce+ct+tag)>（见 app/utils/crypto.py）
    password_enc: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # TLS：disable | prefer | require | verify-ca | verify-full
    ssl_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="prefer")
    # verify-ca / verify-full 时使用的 CA（PEM 文本，落库前与密码同一套加密策略）
    ssl_ca_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        ENUM("connected", "disconnected", "syncing", name="datasource_status", create_type=False),
        nullable=False,
        default="disconnected",
    )
    health_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    health_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    datasources = relationship(
        "DataSource", back_populates="connection", lazy="selectin"
    )
