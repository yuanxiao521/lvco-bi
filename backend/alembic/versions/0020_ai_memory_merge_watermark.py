"""add merge watermark/counters to ai_memories

Revision ID: 0020_ai_memory_merge_watermark
Revises: 0019_add_ai_session_entry
Create Date: 2026-10-07

记忆合并的"水位 + 计数"三列（借鉴 Claude Code session memory compact 的
lastSummarizedMessageId 思路）：

- last_merged_message_id：已并入长期记忆的最后一条消息 id。这是**消息级水位**，
  取代原先"用 covered_rounds 反推第几轮"的做法——后者与"实际并入了哪些内容"脱钩，
  积压时会静默跳过中间段。
- merge_count：成功合并次数（审计）。
- merge_fail_count：连续失败次数（熔断依据：连续失败达阈值后不再重试，
  避免每轮白烧一次摘要 LLM 调用）。

三列均可空或带默认值，不需要回填历史行；历史行的水位为空即表示"从未按水位合并过"，
首次合并会自动按"最早未并入段"重建水位。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0020_ai_memory_merge_watermark"
down_revision = "0019_add_ai_session_entry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_memories",
        sa.Column("last_merged_message_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ai_memories",
        sa.Column("merge_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ai_memories",
        sa.Column("merge_fail_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("ai_memories", "merge_fail_count")
    op.drop_column("ai_memories", "merge_count")
    op.drop_column("ai_memories", "last_merged_message_id")
