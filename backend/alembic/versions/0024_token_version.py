"""add users.token_version / users.is_active（P0 止血：注销与吊销）

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-08

为什么需要这两列
----------------
登出/改密必须能**立刻**让该账号已签发的 JWT 失效。JWT 是无状态的，服务端没有
"已签发令牌清单"，所以用最小成本方案：users.token_version 单调递增，JWT 里携带
同名字段（见 app/core/security.py），鉴权时比对不一致即 401（app/api/deps.py
assert_token_state）。is_active 则用于"停用账号"——停用后既有令牌立刻 403，
而不是等 30 分钟 access token 自然过期。

兼容性
------
两列都带 server_default（0 / true），既有行在迁移时自动填默认值；升级前签发的
老 token 没有 token_version claim，鉴权侧按 0 处理（app/core/security.py
token_version_of），因此**不会把存量用户一次性踢下线**。

注意：down_revision 指向 0023——该 revision 由并行的同事提交；本文件先按其存在
写死，若 0023 尚未合入，alembic upgrade 需要等它到位后才能串起来（见交付说明）。
"""
from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("users", "is_active")
    op.drop_column("users", "token_version")
