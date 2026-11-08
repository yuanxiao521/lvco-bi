# -*- coding: utf-8 -*-
"""agent_calls：工具 / LLM / 轮次调用留痕（P1-C B1-lite）。

一张表承载四类记录（kind = tool | llm | dispatch | turn），
按 (trace_id, seq) 排序即一轮请求的完整时间线；turn 行是一轮收尾汇总
（1 行/轮，保留期比明细长）。

设计来源：docs/research/03-codebase-audit.md §A.3、04-adversarial-review.md §5.1。
- 明细行（tool/llm）30 天后由 agent_trace.purge_old 清理；turn 行长期保留。
- args_json 只存**脱敏后**的参数（白名单键 + SQL 字面量替换为 ?），
  连接串/密码等永不出现在这里（只放行白名单键，其余统一 "<masked>"）。

索引 4 个（任务书写 3 个，多的一个是 turn_id）：
--turn CLI 必须能按 turn_id 查，没有索引会退化成全表扫描；其余 3 个分别是
(trace_id, seq) / (session_id, created_at DESC) / (tool_name, created_at DESC, 仅失败行)。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentCall(Base):
    """单条调用记录（工具 / LLM / 派发 / 轮次汇总）。"""

    __tablename__ = "agent_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # ── 时间线键 ──────────────────────────────────────────────
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── 分类 ──────────────────────────────────────────────────
    kind: Mapped[str] = mapped_column(String(12), nullable=False)      # tool|llm|dispatch|turn
    entry: Mapped[str] = mapped_column(String(12), nullable=False)     # chat|canvas
    mode: Mapped[str | None] = mapped_column(String(24), nullable=True)
    parent: Mapped[str | None] = mapped_column(String(24), nullable=True)

    # ── 关联 ──────────────────────────────────────────────────
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ai_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ── tool 类 ───────────────────────────────────────────────
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    args_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    args_hash: Mapped[str | None] = mapped_column(CHAR(8), nullable=True)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_layer: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, default=1)
    memo_hit: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    # ── llm 类 ────────────────────────────────────────────────
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_cached: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── turn 汇总 ─────────────────────────────────────────────
    degraded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rounds: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    tool_calls: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    tool_failures: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
