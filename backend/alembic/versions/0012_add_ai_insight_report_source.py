"""add ai_insight to report_source_type enum

Revision ID: 0012_add_ai_insight_report_source
Revises: 0011_add_operation_log
Create Date: 2026-07-27

新增 ai_insight 枚举值，用于 InsightRunner 自动生成的洞察日报 Report。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0012_add_ai_insight_report_source"
down_revision: Union[str, None] = "0011_add_operation_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE 必须在事务外执行（PG 限制），用 autocommit_block 包裹
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE report_source_type ADD VALUE IF NOT EXISTS 'ai_insight'")


def downgrade() -> None:
    # PostgreSQL 不支持直接 REMOVE VALUE，downgrade 为空操作
    # 如需回滚需手动重建 enum 类型，这里不做
    pass
