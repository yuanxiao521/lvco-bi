"""ai_messages.steps：消息级过程步骤投影（从 agent_calls 事实源生成）

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-09

定位
----
agent_calls 是事实源（结构化、可 SQL、30 天明细）；本列是它的 UI 物化视图：
turn 收尾时生成一份步骤摘要随消息落库，历史消息展开过程时零查询成本，
且不受 agent_calls 明细 30 天清理影响（消息在，投影就在）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_messages",
        sa.Column("steps", postgresql.JSON, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_messages", "steps")
