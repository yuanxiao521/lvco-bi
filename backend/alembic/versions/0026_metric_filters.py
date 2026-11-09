"""指标级范围口径 metric_filters（口径 = 聚合 + 过滤）

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-09

设计
----
- filters 与 metric_definitions 级联删除（口径整体版本化，不留下孤儿条件）；
- value 用 JSON：in/between 是数组，其余标量；op 白名单在应用层校验
  （与 query_engine.ALLOWED_OPERATORS 一致），数据库层不做枚举约束以便扩展。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_filters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "metric_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("metric_definitions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field", sa.String(length=200), nullable=False),
        sa.Column("op", sa.String(length=20), nullable=False),
        sa.Column("value", postgresql.JSON, nullable=False),
    )
    op.create_index("ix_metric_filters_metric", "metric_filters", ["metric_id"])


def downgrade() -> None:
    op.drop_index("ix_metric_filters_metric", table_name="metric_filters")
    op.drop_table("metric_filters")
