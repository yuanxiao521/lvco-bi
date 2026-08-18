"""extend chart_type enum with funnel/heatmap/radar/sankey

Revision ID: 0003_extend_chart_type
Revises: 0002_add_manual_source_type
Create Date: 2026-07-26

注意：PostgreSQL 的 ALTER TYPE ... ADD VALUE 不能在同一事务内被引用，
因此本 migration 自身不能在事务内（autocommit）。
若数据库版本 < 12，只能逐条执行；>= 12 支持一次多值。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_extend_chart_type"
down_revision: Union[str, None] = "0002_add_manual_source_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ADD VALUE 不能在事务内运行，使用 autocommit
    with op.get_context().autocommit_block():
        for value in ("funnel", "heatmap", "radar", "sankey"):
            op.execute(f"ALTER TYPE chart_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL 不支持 DROP ENUM VALUE，只能手动删表后重建
    # 这里提供 no-op 并提示：见 init.sql 重建流程
    pass