"""extend chart_type enum with horizontal_bar

Revision ID: 0010_add_horizontal_bar
Revises: 0009_add_insight_tables
Create Date: 2026-07-27

新增 horizontal_bar（水平条形图）枚举值，便于在长类别名 / 排名场景下使用。
前端 ECharts 通过 exchangeAxis 实现水平条形；后端 matplotlib 通过 barh 渲染。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010_add_horizontal_bar"
down_revision: Union[str, None] = "0009_add_insight_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE chart_type ADD VALUE IF NOT EXISTS 'horizontal_bar'")


def downgrade() -> None:
    pass
