"""add manual to report_source_type

Revision ID: 0002_add_manual_source_type
Revises: 0001_init
Create Date: 2026-07-23

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_add_manual_source_type"
down_revision: Union[str, None] = "0001_init"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
