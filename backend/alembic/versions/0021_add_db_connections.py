"""add db_connections (一份凭据 -> 多张表)

Revision ID: 0021_add_db_connections
Revises: 0020_ai_memory_merge_watermark
Create Date: 2026-11-02

背景：改造前「连一张表 = 一个 datasource 行 = 一份密码密文」，选 3 张表就有 3 份
凭据副本，轮换密码没有原子性。本迁移把凭据与连接参数上收到 db_connections，
datasources 只保留 connection_id（凭据一份、表映射多份）。

数据迁移规则：按 (user_id, source_type, host, port, db_name, username, password)
去重生成连接行，再把 datasource 关联过去，最后把 datasource.connection_config 里的
password 字段删掉（保留 table_name / db_name 等非敏感字段，供旧代码路径继续读）。
"""
from alembic import op


revision = "0021_add_db_connections"
down_revision = "0020_ai_memory_merge_watermark"
branch_labels = None
depends_on = None


# 端口在 JSON 里可能是字符串、数字或缺省，这里统一成"能转就转，否则按类型默认"
_PORT = (
    "CASE WHEN COALESCE(d.connection_config->>'port', '') ~ '^[0-9]+$' "
    "THEN (d.connection_config->>'port')::int "
    "ELSE (CASE WHEN d.source_type = 'mysql' THEN 3306 ELSE 5432 END) END"
)
_HOST = "COALESCE(NULLIF(d.connection_config->>'host', ''), 'localhost')"
_DB = "COALESCE(NULLIF(d.connection_config->>'db_name', ''), '')"
_USER = (
    "COALESCE(NULLIF(d.connection_config->>'username', ''), "
    "CASE WHEN d.source_type = 'mysql' THEN 'root' ELSE 'postgres' END)"
)
_PWD = "COALESCE(d.connection_config->>'password', '')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS db_connections (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name VARCHAR(200) NOT NULL,
            source_type source_type NOT NULL,
            host VARCHAR(253) NOT NULL,
            port INTEGER NOT NULL,
            db_name VARCHAR(128) NOT NULL DEFAULT '',
            username VARCHAR(128) NOT NULL DEFAULT '',
            password_enc VARCHAR(1024),
            ssl_mode VARCHAR(20) NOT NULL DEFAULT 'prefer',
            ssl_ca_enc TEXT,
            status datasource_status NOT NULL DEFAULT 'disconnected',
            health_status VARCHAR(16),
            health_error VARCHAR(300),
            last_health_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_db_connections_user_id ON db_connections(user_id)"
    )
    op.execute(
        "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS connection_id UUID "
        "REFERENCES db_connections(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_datasources_connection_id ON datasources(connection_id)"
    )

    # 1) 按物理连接去重出连接行（历史明文/旧密文原样搬过来；格式升级交给
    #    scripts/migrate_datasource_secrets.py，避免迁移脚本耦合加密实现）
    op.execute(
        f"""
        INSERT INTO db_connections
            (user_id, name, source_type, host, port, db_name, username,
             password_enc, ssl_mode, status, created_at, updated_at)
        SELECT d.user_id,
               COALESCE(NULLIF(d.connection_config->>'db_name', ''), 'database'),
               d.source_type,
               {_HOST},
               {_PORT},
               {_DB},
               {_USER},
               NULLIF({_PWD}, ''),
               'prefer',
               'disconnected',
               now(),
               now()
          FROM datasources d
         WHERE d.source_type IN ('postgresql', 'mysql')
           AND d.connection_config IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM db_connections c
                  WHERE c.user_id = d.user_id
                    AND c.source_type = d.source_type
                    AND c.host = {_HOST}
                    AND c.port = {_PORT}
                    AND c.db_name = {_DB}
                    AND c.username = {_USER}
                    AND COALESCE(c.password_enc, '') = {_PWD}
               )
        """
    )

    # 2) datasource 关联到连接
    op.execute(
        f"""
        UPDATE datasources d
           SET connection_id = c.id
          FROM db_connections c
         WHERE d.connection_id IS NULL
           AND d.source_type IN ('postgresql', 'mysql')
           AND d.connection_config IS NOT NULL
           AND c.user_id = d.user_id
           AND c.source_type = d.source_type
           AND c.host = {_HOST}
           AND c.port = {_PORT}
           AND c.db_name = {_DB}
           AND c.username = {_USER}
           AND COALESCE(c.password_enc, '') = {_PWD}
        """
    )

    # 3) 凭据不再留在 datasource 行上
    op.execute(
        """
        UPDATE datasources
           SET connection_config = connection_config - 'password'
         WHERE source_type IN ('postgresql', 'mysql')
           AND connection_config ? 'password'
        """
    )


def downgrade() -> None:
    # 把密码写回 datasource.connection_config（尽力而为，仅供回滚）
    op.execute(
        """
        UPDATE datasources d
           SET connection_config = COALESCE(d.connection_config, '{}'::jsonb)
                                  || jsonb_build_object('password', c.password_enc)
          FROM db_connections c
         WHERE d.connection_id = c.id
           AND c.password_enc IS NOT NULL
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_datasources_connection_id")
    op.execute("ALTER TABLE datasources DROP COLUMN IF EXISTS connection_id")
    op.execute("DROP TABLE IF EXISTS db_connections")
