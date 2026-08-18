"""add favorites table

Revision ID: 0004_add_favorites_table
Revises: 0003_extend_chart_type
Create Date: 2026-07-26

收藏夹表：用户对任意资源（画布/仪表盘/报表/数据源）的快捷访问标记。
- user_id + item_type + item_id 联合唯一约束防止重复收藏
- item_title 冗余存储，原资源被删后收藏仍可显示
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_add_favorites_table"
down_revision: Union[str, None] = "0003_extend_chart_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ENUM 类型
    favorite_item_type = postgresql.ENUM(
        "canvas", "dashboard", "report", "datasource",
        name="favorite_item_type",
    )
    favorite_item_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "favorites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_type",
            postgresql.ENUM(
                "canvas", "dashboard", "report", "datasource",
                name="favorite_item_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("item_id", sa.String(100), nullable=False),
        sa.Column("item_title", sa.String(200), nullable=False),
        sa.Column("item_meta", postgresql.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_favorites_user_id", "favorites", ["user_id"])
    op.create_index("ix_favorites_user_created", "favorites", ["user_id", "created_at"])
    op.create_unique_constraint(
        "uq_favorites_user_item", "favorites", ["user_id", "item_type", "item_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_favorites_user_item", "favorites", type_="unique")
    op.drop_index("ix_favorites_user_created", table_name="favorites")
    op.drop_index("ix_favorites_user_id", table_name="favorites")
    op.drop_table("favorites")
    op.execute("DROP TYPE IF EXISTS favorite_item_type")