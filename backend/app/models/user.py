import enum
import uuid

from sqlalchemy import Boolean, Enum, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class UserRole(str, enum.Enum):
    admin = "admin"
    editor = "editor"
    viewer = "viewer"


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.editor, nullable=False
    )
    # ── P0 止血：注销与吊销 ──────────────────────────────────────────────
    # token_version：每次登出/改密 +1。JWT 里携带同名字段，get_current_user 比对
    # 不一致即 401 —— 无状态 JWT 没有"服务端会话表"，这是最小的"全部已签发令牌作废"手段。
    # 默认 0 + server_default：老 token 没有该 claim 时按 0 处理，升级不会把所有人踢下线。
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # is_active：账号是否可登录/可用。停用后既有 token 也立刻不可用（403）。
    # 同样给 server_default，避免既有行在迁移后违反 NOT NULL。
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    datasources = relationship("DataSource", back_populates="user", cascade="all, delete-orphan")
    canvases = relationship("Canvas", back_populates="user", cascade="all, delete-orphan")
    dashboards = relationship("Dashboard", back_populates="user", cascade="all, delete-orphan")
    reports = relationship("Report", back_populates="user", cascade="all, delete-orphan")
    ai_sessions = relationship("AISession", back_populates="user", cascade="all, delete-orphan")
    preferences = relationship("UserPreference", back_populates="user", cascade="all, delete-orphan")
