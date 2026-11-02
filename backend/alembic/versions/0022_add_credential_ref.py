"""add credential_ref to db_connections (服务端凭据引用)

Revision ID: 0022_add_credential_ref
Revises: 0021_add_db_connections
Create Date: 2026-11-02

B 方案：凭据来源从"用户在前端输入"扩展为"可由部署侧注入"。
credential_ref 形如 env:LVCO_PG_PASSWORD / file:/run/secrets/pg_pw，
与 password_enc 互斥；解析器见 app/services/credential_refs.py。
"""
from alembic import op
import sqlalchemy as sa


revision = "0022_add_credential_ref"
down_revision = "0021_add_db_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "db_connections",
        sa.Column("credential_ref", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("db_connections", "credential_ref")
