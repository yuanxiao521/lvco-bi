"""add user_preferences table

Revision ID: 0013_add_user_preferences
Revises: 0012_add_ai_insight_report_source
Create Date: 2026-08-04

添加用户偏好记忆表，支持 Agent 记忆用户偏好（图表类型、配色、分析维度等）
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0013_add_user_preferences'
down_revision = '0012_add_ai_insight_report_source'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_preferences',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('preference_type', sa.String(50), nullable=False),
        sa.Column('preference_key', sa.String(100), nullable=False),
        sa.Column('preference_value', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('strength', sa.Float(), nullable=False, server_default='0.5'),
        sa.Column('evidence_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    
    # 创建索引
    op.create_index('ix_user_preferences_user_id', 'user_preferences', ['user_id'])
    op.create_index('ix_user_preferences_type_key', 'user_preferences', ['preference_type', 'preference_key'])
    op.create_index('ix_user_preferences_strength', 'user_preferences', ['strength'])


def downgrade() -> None:
    op.drop_index('ix_user_preferences_strength', table_name='user_preferences')
    op.drop_index('ix_user_preferences_type_key', table_name='user_preferences')
    op.drop_index('ix_user_preferences_user_id', table_name='user_preferences')
    op.drop_table('user_preferences')
