"""extend chart_type enum with grouped_bar/stacked_bar/kpi_card

Revision ID: 0008_extend_chart_type_phase4
Revises: 0007_add_render_config
Create Date: 2026-07-27

模型中已加入 grouped_bar / stacked_bar / kpi_card 三个新图表类型，
但之前只迁移了 funnel/heatmap/radar/sankey，导致保存到仪表盘时
asyncpg 抛 `invalid input value for enum chart_type: "grouped_bar"`。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0008_extend_chart_type_phase4"
down_revision: Union[str, None] = "0007_add_render_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in ("grouped_bar", "stacked_bar", "kpi_card"):
            op.execute(f"ALTER TYPE chart_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    pass