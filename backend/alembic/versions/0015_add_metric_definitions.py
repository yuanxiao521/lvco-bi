"""add metric_definitions table (指标语义层)

Revision ID: 0015_add_metric_definitions
Revises: 0014_add_datasource_description
Create Date: 2026-08-20

引入指标中心核心实体：指标定义（MetricDefinition），沉淀命名指标口径
（如 销售额 = SUM("amount")），供画布块 / 查询 / AI 通过 metric_id 引用。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0015_add_metric_definitions"
down_revision = "0014_add_datasource_description"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, default=None),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        sa.Column("formula", sa.Text, nullable=False),
        sa.Column("agg_kind", sa.String(20), nullable=True),
        sa.Column(
            "datasource_id",
            UUID(as_uuid=True),
            sa.ForeignKey("datasources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("table_ref", sa.String(200), nullable=True, default="data"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("key", "user_id", name="uq_metrics_key_user"),
    )
    op.create_index("idx_metric_user", "metric_definitions", ["user_id"])
    op.create_index("idx_metric_datasource", "metric_definitions", ["datasource_id"])


def downgrade() -> None:
    op.drop_index("idx_metric_datasource", table_name="metric_definitions")
    op.drop_index("idx_metric_user", table_name="metric_definitions")
    op.drop_table("metric_definitions")