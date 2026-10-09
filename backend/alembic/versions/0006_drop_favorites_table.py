"""drop favorites table

Revises: 0005_add_soft_delete
Creates: None (drop)

收藏夹功能下线，移除 favorites 表及相关索引/约束。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006_drop_favorites_table"
down_revision: Union[str, None] = "0005_add_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("favorites")


def downgrade() -> None:
    # 不重建：收藏夹已废弃，避免误导
    pass
