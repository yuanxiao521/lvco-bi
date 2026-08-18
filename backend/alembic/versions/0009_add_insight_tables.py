"""add insight tables

Revision ID: 0009_add_insight_tables
Revises: 0008_extend_chart_type_phase4
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY


revision = "0009_add_insight_tables"
down_revision = "0008_extend_chart_type_phase4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # insight_rules
    op.create_table(
        "insight_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("query_config", JSONB, nullable=False),
        sa.Column("detect_types", ARRAY(sa.String(50)), nullable=False, server_default="{anomaly,trend,ratio}"),
        sa.Column("threshold", JSONB, nullable=True),
        sa.Column("report_type", sa.String(30), nullable=False, server_default="daily_report"),
        sa.Column("schedule", sa.String(20), nullable=False, server_default="daily"),
        sa.Column("schedule_time", sa.Time, nullable=False, server_default="09:00:00"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("TRUE")),
        sa.Column("auto_created", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(20), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_insight_rules_user_enabled",
        "insight_rules",
        ["user_id", "enabled"],
        postgresql_where=sa.text("enabled = TRUE"),
    )

    # insight_records
    op.create_table(
        "insight_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", UUID(as_uuid=True), sa.ForeignKey("insight_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("ai_narrative", sa.Text, nullable=True),
        sa.Column("charts", JSONB, nullable=True),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("detected_anomalies", JSONB, nullable=True),
        sa.Column("llm_model", sa.String(50), nullable=True),
        sa.Column("llm_tokens_input", sa.Integer, nullable=True),
        sa.Column("llm_tokens_output", sa.Integer, nullable=True),
        sa.Column("report_id", UUID(as_uuid=True), sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_insight_records_user_run_at",
        "insight_records",
        ["user_id", sa.text("run_at DESC")],
    )

    # insight_suggestions
    op.create_table(
        "insight_suggestions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_name", sa.String(200), nullable=False),
        sa.Column("time_field", sa.String(200), nullable=True),
        sa.Column("measure_fields", ARRAY(sa.String(200)), nullable=True),
        sa.Column("dimension_fields", ARRAY(sa.String(200)), nullable=True),
        sa.Column("suggested_name", sa.String(100), nullable=True),
        sa.Column("suggested_config", JSONB, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("row_count_estimate", sa.Integer, nullable=True),
        sa.Column("update_frequency", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("accepted_rule_id", UUID(as_uuid=True), sa.ForeignKey("insight_rules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_insight_suggestions_user_status",
        "insight_suggestions",
        ["user_id", "status", sa.text("created_at DESC")],
    )

    # notifications
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("link_url", sa.String(500), nullable=True),
        sa.Column("resource_type", sa.String(30), nullable=True),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("read", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_notifications_user_unread",
        "notifications",
        ["user_id", "read", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("insight_suggestions")
    op.drop_table("insight_records")
    op.drop_table("insight_rules")
