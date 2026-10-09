"""init

Revision ID: 0001_init
Revises:
Create Date: 2026-07-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_init"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TYPE IF EXISTS user_role")
    op.execute("DROP TYPE IF EXISTS source_type")
    op.execute("DROP TYPE IF EXISTS datasource_status")
    op.execute("DROP TYPE IF EXISTS chart_type")
    op.execute("DROP TYPE IF EXISTS report_status")
    op.execute("DROP TYPE IF EXISTS report_source_type")
    op.execute("DROP TYPE IF EXISTS ai_message_role")

    op.execute("CREATE TYPE user_role AS ENUM ('admin', 'editor', 'viewer')")
    op.execute("CREATE TYPE source_type AS ENUM ('csv', 'excel', 'mysql', 'postgresql')")
    op.execute("CREATE TYPE datasource_status AS ENUM ('connected', 'disconnected', 'syncing')")
    op.execute("CREATE TYPE chart_type AS ENUM ('bar', 'line', 'pie', 'scatter', 'area', 'donut')")
    op.execute("CREATE TYPE report_status AS ENUM ('draft', 'published', 'shared')")
    op.execute("CREATE TYPE report_source_type AS ENUM ('canvas', 'dashboard', 'manual')")
    op.execute("CREATE TYPE ai_message_role AS ENUM ('user', 'assistant')")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("role", postgresql.ENUM("admin", "editor", "viewer", name="user_role", create_type=False), server_default="editor", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_users_email", "users", ["email"])

    op.create_table(
        "datasources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_type", postgresql.ENUM("csv", "excel", "mysql", "postgresql", name="source_type", create_type=False), nullable=False),
        sa.Column("connection_config", postgresql.JSONB, nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("schema_meta", postgresql.JSONB, nullable=True),
        sa.Column("status", postgresql.ENUM("connected", "disconnected", "syncing", name="datasource_status", create_type=False), server_default="disconnected", nullable=False),
        sa.Column("size_bytes", sa.BigInteger, server_default="0", nullable=False),
        sa.Column("row_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_datasources_user_id", "datasources", ["user_id"])
    op.create_index("idx_datasources_status", "datasources", ["status"])

    op.create_table(
        "canvases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("table_name", sa.String(200), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("blocks", postgresql.JSONB, server_default="[]", nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_canvases_user_id", "canvases", ["user_id"])
    op.create_index("idx_canvases_datasource_id", "canvases", ["datasource_id"])

    op.create_table(
        "chart_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("datasource_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chart_type", postgresql.ENUM("bar", "line", "pie", "scatter", "area", "donut", name="chart_type", create_type=False), nullable=False),
        sa.Column("query_config", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_chart_configs_datasource_id", "chart_configs", ["datasource_id"])

    op.create_table(
        "dashboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("layout", postgresql.JSONB, server_default="[]", nullable=True),
        sa.Column("refresh_interval", sa.Integer, server_default="300", nullable=False),
        sa.Column("is_public", sa.Boolean, server_default="false", nullable=False),
        sa.Column("share_token", sa.String(100), unique=True, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_dashboards_user_id", "dashboards", ["user_id"])
    op.create_index("idx_dashboards_share_token", "dashboards", ["share_token"])

    op.create_table(
        "dashboard_charts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("dashboard_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chart_config_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chart_configs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("position", postgresql.JSONB, server_default='{"x": 0, "y": 0, "w": 1, "h": 1}', nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_dashboard_charts_dashboard_id", "dashboard_charts", ["dashboard_id"])
    op.create_index("idx_dashboard_charts_config_id", "dashboard_charts", ["chart_config_id"])

    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source_type", postgresql.ENUM("canvas", "dashboard", "manual", name="report_source_type", create_type=False), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("snapshot_blocks", postgresql.JSONB, nullable=True),
        sa.Column("status", postgresql.ENUM("draft", "published", "shared", name="report_status", create_type=False), server_default="draft", nullable=False),
        sa.Column("share_token", sa.String(100), unique=True, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
    )
    op.create_index("idx_reports_user_id", "reports", ["user_id"])
    op.create_index("idx_reports_source", "reports", ["source_type", "source_id"])

    op.create_table(
        "ai_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model", sa.String(50), server_default="gpt-4o", nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_ai_sessions_user_id", "ai_sessions", ["user_id"])

    op.create_table(
        "ai_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ai_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", postgresql.ENUM("user", "assistant", name="ai_message_role", create_type=False), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("chart_data", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_ai_messages_session_id", "ai_messages", ["session_id"])


def downgrade() -> None:
    op.drop_table("ai_messages")
    op.drop_table("ai_sessions")
    op.drop_table("reports")
    op.drop_table("dashboard_charts")
    op.drop_table("dashboards")
    op.drop_table("chart_configs")
    op.drop_table("canvases")
    op.drop_table("datasources")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS ai_message_role")
    op.execute("DROP TYPE IF EXISTS report_source_type")
    op.execute("DROP TYPE IF EXISTS report_status")
    op.execute("DROP TYPE IF EXISTS chart_type")
    op.execute("DROP TYPE IF EXISTS datasource_status")
    op.execute("DROP TYPE IF EXISTS source_type")
    op.execute("DROP TYPE IF EXISTS user_role")
