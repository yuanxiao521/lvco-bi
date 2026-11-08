"""add agent_calls（P1-C B1-lite：工具/LLM/轮次调用留痕）

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-08

为什么一张表承载四类记录
------------------------
一轮请求要回答"为什么没出图 / 慢在哪 / 被哪层拦了 / 花了多少钱"，需要把
tool / llm / dispatch / turn 四类事实按**同一条时间线**排开。拆多表就要 join，
查询与回放都变复杂；一张表 + (trace_id, seq) 排序即可（见 03-codebase-audit §A.3）。

保留策略
--------
明细行（tool/llm）由 app/services/agent_trace.py:purge_old 按
AGENT_CALL_RETAIN_DAYS（默认 30 天）清理；turn 行（1 行/轮）长期保留，
用于回答"每轮几轮/几次工具/几次失败/是否降级"。

索引
----
1) (trace_id, seq)                      —— 一轮请求的完整时间线（CLI --trace）
2) (turn_id, seq)                       —— 跨多次请求的一轮（HITL 续跑）时间线（CLI --turn）
3) (session_id, created_at DESC)        —— 按会话看每轮汇总
4) (tool_name, created_at DESC) WHERE ok=false —— "哪个工具最容易失败"
（任务书要求 3 个；turn_id 索引是为 --turn CLI 补的第 4 个，否则会退化成全表扫描。）
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        # 时间线键
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        # 分类
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("entry", sa.String(length=12), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=True),
        sa.Column("parent", sa.String(length=24), nullable=True),
        # 关联（SET NULL：审计记录不该因为删会话/删用户而消失）
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # tool 类
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("args_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("args_hash", sa.CHAR(length=8), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("error_layer", sa.SmallInteger(), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("result_chars", sa.Integer(), nullable=True),
        sa.Column("result_preview", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.SmallInteger(), nullable=True, server_default=sa.text("1")),
        sa.Column("memo_hit", sa.Boolean(), nullable=True, server_default=sa.text("false")),
        # llm 类
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("tokens_cached", sa.Integer(), nullable=True),
        # turn 汇总
        sa.Column("degraded", sa.Boolean(), nullable=True),
        sa.Column("rounds", sa.SmallInteger(), nullable=True),
        sa.Column("tool_calls", sa.SmallInteger(), nullable=True),
        sa.Column("tool_failures", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_index("idx_agent_calls_trace", "agent_calls", ["trace_id", "seq"])
    op.create_index("idx_agent_calls_turn", "agent_calls", ["turn_id", "seq"])
    op.create_index(
        "idx_agent_calls_session", "agent_calls", ["session_id", sa.text("created_at DESC")]
    )
    op.create_index(
        "idx_agent_calls_tool",
        "agent_calls",
        ["tool_name", sa.text("created_at DESC")],
        postgresql_where=sa.text("ok = false"),
    )


def downgrade() -> None:
    op.drop_index("idx_agent_calls_tool", table_name="agent_calls")
    op.drop_index("idx_agent_calls_session", table_name="agent_calls")
    op.drop_index("idx_agent_calls_turn", table_name="agent_calls")
    op.drop_index("idx_agent_calls_trace", table_name="agent_calls")
    op.drop_table("agent_calls")
