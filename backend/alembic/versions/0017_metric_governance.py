"""add metric governance tables and metric definition fields

Revision ID: 0017_metric_governance
Revises: 0016_dashboard_refresh
Create Date: 2026-08-20

指标治理升级（Spec 2 Task 1）：
- metric_definitions：新增 version / formula_type / depends_on_metric_ids 字段
- metric_lineage：血缘（指标 -> 源字段 -> 目标字段 + transform）
- metric_usage：使用追踪（dashboard / canvas / ai_query）
- metric_versions：版本快照（每次口径变更保留完整定义 + 变更说明）
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017_metric_governance"
down_revision = "0016_dashboard_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "metric_definitions",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "metric_definitions",
        sa.Column(
            "formula_type",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'basic'"),
        ),
    )
    op.add_column(
        "metric_definitions",
        sa.Column("depends_on_metric_ids", postgresql.JSONB(), nullable=True),
    )

    op.create_table(
        "metric_lineage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_field", sa.String(200), nullable=False),
        sa.Column("transform", sa.String(200), nullable=True),
        sa.Column("target_field", sa.String(200), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"], ["metric_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["datasources.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_metric_lineage_metric_source",
        "metric_lineage",
        ["metric_id", "source_field"],
    )

    op.create_table(
        "metric_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("usage_type", sa.String(32), nullable=False),
        sa.Column("usage_ref_id", sa.String(64), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["metric_id"], ["metric_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_metric_usage_metric_type",
        "metric_usage",
        ["metric_id", "usage_type"],
    )
    op.create_index(
        "ix_metric_usage_type_ref",
        "metric_usage",
        ["usage_type", "usage_ref_id"],
    )

    op.create_table(
        "metric_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"], ["metric_definitions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "metric_id", "version", name="uq_metric_versions_metric_version"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_metric_versions_metric_id",
        "metric_versions",
        ["metric_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_metric_versions_metric_id", table_name="metric_versions")
    op.drop_table("metric_versions")

    op.drop_index("ix_metric_usage_type_ref", table_name="metric_usage")
    op.drop_index("ix_metric_usage_metric_type", table_name="metric_usage")
    op.drop_table("metric_usage")

    op.drop_index("ix_metric_lineage_metric_source", table_name="metric_lineage")
    op.drop_table("metric_lineage")

    op.drop_column("metric_definitions", "depends_on_metric_ids")
    op.drop_column("metric_definitions", "formula_type")
    op.drop_column("metric_definitions", "version")
