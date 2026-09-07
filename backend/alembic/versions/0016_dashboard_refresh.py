"""add dashboard refresh scheduling fields

Revision ID: 0016_dashboard_refresh
Revises: 0015_add_metric_definitions
Create Date: 2026-08-20

为 Dashboard 引入定时刷新能力：
- refresh_cron: cron 表达式（最大 64 字符，可空）
- refresh_enabled: 是否启用定时刷新（默认 False）
- last_refreshed_at: 上次刷新时间（用于洞察/报告触发与展示）

scheduler 将按 refresh_enabled=True 过滤加载待执行的 Dashboard。
"""
from alembic import op
import sqlalchemy as sa


revision = "0016_dashboard_refresh"
down_revision = "0015_add_metric_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column("refresh_cron", sa.String(64), nullable=True),
    )
    op.add_column(
        "dashboards",
        sa.Column(
            "refresh_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "dashboards",
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_dashboards_refresh_enabled",
        "dashboards",
        ["refresh_enabled"],
    )


def downgrade() -> None:
    op.drop_index("idx_dashboards_refresh_enabled", table_name="dashboards")
    op.drop_column("dashboards", "last_refreshed_at")
    op.drop_column("dashboards", "refresh_enabled")
    op.drop_column("dashboards", "refresh_cron")