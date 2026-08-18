"""add render_config to chart_configs

Revision ID: 0007_add_render_config
Revises: 0006_drop_favorites_table
Create Date: 2026-07-26

图表渲染配置（renderer/palette）保存到 chart_configs.render_config 列，
与 query_config（维度/度量/筛选）分离，方便复用同一查询而切换渲染器。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_add_render_config"
down_revision: Union[str, None] = "0006_drop_favorites_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chart_configs",
        sa.Column("render_config", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chart_configs", "render_config")