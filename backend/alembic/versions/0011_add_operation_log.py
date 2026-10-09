"""add operation_log table

Revision ID: 0011_add_operation_log
Revises: 0010_add_horizontal_bar
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


revision = "0011_add_operation_log"
down_revision = "0010_add_horizontal_bar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("resource_type", sa.String(40), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=True),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.Text, nullable=True),
        sa.Column("extra", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("idx_operation_logs_user_time", "operation_logs", ["user_id", sa.text("created_at DESC")])
    op.create_index("idx_operation_logs_action", "operation_logs", ["action", sa.text("created_at DESC")])
    op.create_index("idx_operation_logs_resource", "operation_logs", ["resource_type", "resource_id"])


def downgrade() -> None:
    op.drop_index("idx_operation_logs_resource", table_name="operation_logs")
    op.drop_index("idx_operation_logs_action", table_name="operation_logs")
    op.drop_index("idx_operation_logs_user_time", table_name="operation_logs")
    op.drop_table("operation_logs")
