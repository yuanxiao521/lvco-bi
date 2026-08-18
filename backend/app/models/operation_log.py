"""操作日志模型：记录用户的关键操作（登录、CRUD、AI 调用等）。

设计为独立中间件写入，不侵入业务代码。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OperationLog(Base):
    """用户操作审计日志。"""

    __tablename__ = "operation_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # user_id 可为空：登录失败、token 失效时无法解析用户
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # 业务操作名：login / canvas.create / canvas.delete / datasource.update / ai.query ...
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # 资源类型：auth / canvas / datasource / dashboard / ai / user
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # 资源 ID（如 canvas uuid），可空
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 自由扩展：操作上下文、错误信息摘要、目标资源名称等
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
