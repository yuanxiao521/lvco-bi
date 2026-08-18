"""add deleted_at to canvases and dashboards + add deleted status to reports

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0005_add_soft_delete'
down_revision: Union[str, None] = '0004_add_favorites_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add deleted_at to canvases
    op.add_column('canvases',
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True)
    )

    # Add deleted_at to dashboards
    op.add_column('dashboards',
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True)
    )

    # Add 'deleted' to report_status enum if not already present
    # PostgreSQL: ALTER TYPE ... ADD VALUE
    op.execute("COMMIT")
    op.execute("ALTER TYPE report_status ADD VALUE IF NOT EXISTS 'deleted'")


def downgrade() -> None:
    op.drop_column('canvases', 'deleted_at')
    op.drop_column('dashboards', 'deleted_at')
    # Cannot remove enum value in PostgreSQL
