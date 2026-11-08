"""share link controls: add share_expires_at to dashboards / reports

Revision ID: 0023
Revises: 0022_add_credential_ref
Create Date: 2026-11-08

P0 止血（评审 D3）：分享链接此前**永久有效且不可撤销**，接口返回的 expiresAt 是
now()+30d 现编的（dashboards.py:305 / reports.py:167），数据库里根本没有这一列。

本迁移只做一件事：给 dashboards / reports 各加一列 share_expires_at。
  - NULL   = 永久有效（语义与旧数据一致，回滚安全）
  - 非 NULL = 到点即失效（public.py 判定，失效一律 404）

撤销分享复用既有的 share_token 列（置 NULL）+ is_public（dashboard）/status（report），
**不新增"撤销"状态列**——避免同一事实出现第二套真相源。

关于 down_revision 的说明（重要，别改回去）：
任务书里写的是 down_revision="0022"（简写）。当前 head 的真实 revision id 是
"0022_add_credential_ref"（见 alembic/versions/0022_add_credential_ref.py:15）。
若按简写 "0022" 落库，alembic 会报 "Can't locate revision identified by '0022'"，
整条链断掉、upgrade head 直接失败。故这里写全名。revision 仍按分配值 "0023"。
"""
from alembic import op
import sqlalchemy as sa


revision = "0023"
down_revision = "0022_add_credential_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dashboards",
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reports",
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("reports", "share_expires_at")
    op.drop_column("dashboards", "share_expires_at")
