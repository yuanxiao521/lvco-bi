"""add description column to datasources

Revision ID: 0014_add_datasource_description
Revises: 0013_add_user_preferences
Create Date: 2026-08-19

添加数据源描述字段，支持用户自定义表描述，用于 Agent 理解数据源用途
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0014_add_datasource_description'
down_revision = '0013_add_user_preferences'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('datasources', sa.Column('description', sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column('datasources', 'description')
