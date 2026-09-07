"""add ai_memories table (session-level compressed memory)

Revision ID: 0018_add_ai_memory
Revises: 0017_metric_governance
Create Date: 2026-08-31

会话级压缩记忆：每轮结束把较早历史压缩成摘要持久化，下一轮注入上下文回流。
每会话一行（session_id 唯一约束，upsert 语义）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0018_add_ai_memory"
down_revision = "0017_metric_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_memories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("ai_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("covered_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint("session_id", name="uq_ai_memories_session"),
    )
    op.create_index("idx_ai_memories_session", "ai_memories", ["session_id"])


def downgrade() -> None:
    op.drop_index("idx_ai_memories_session", table_name="ai_memories")
    op.drop_table("ai_memories")
