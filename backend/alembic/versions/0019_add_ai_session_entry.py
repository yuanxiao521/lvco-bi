"""add entry/canvas_id to ai_sessions (entry isolation)

Revision ID: 0019_add_ai_session_entry
Revises: 0018_add_ai_memory
Create Date: 2026-09-09

会话入口隔离：ai_sessions 增加 entry（chat/canvas）与 canvas_id（归属画布）。
旧会话由 server_default 统一标记为 chat，无需数据回填。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0019_add_ai_session_entry"
down_revision = "0018_add_ai_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_sessions",
        sa.Column(
            "entry",
            sa.String(20),
            nullable=False,
            server_default="chat",
        ),
    )
    op.add_column(
        "ai_sessions",
        sa.Column("canvas_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "idx_ai_sessions_canvas",
        "ai_sessions",
        ["entry", "canvas_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_ai_sessions_canvas", table_name="ai_sessions")
    op.drop_column("ai_sessions", "canvas_id")
    op.drop_column("ai_sessions", "entry")