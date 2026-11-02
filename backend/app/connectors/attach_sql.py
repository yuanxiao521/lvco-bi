"""DuckDB 外部库 ATTACH 语句构造（PostgreSQL / MySQL）。

设计要点（详见 app/services/datasource_connection.py 的模块注释）：
- 凭据走 DuckDB SECRET，不拼进 ATTACH 连接串 —— 否则密码会留在 catalog 元数据里，
  SELECT path FROM duckdb_databases() 可直接读出明文；
- 一律 READ_ONLY：外部分析源在引擎层就是只读的；
- 所有字面量/标识符都做标准转义，用户输入的 host/user/db/table 不构成 SQL 注入面。
"""
import hashlib

PG_DEFAULT_PORT = 5432
MYSQL_DEFAULT_PORT = 3306


def default_port(source_type: str) -> int:
    return MYSQL_DEFAULT_PORT if str(source_type).lower() == "mysql" else PG_DEFAULT_PORT


def is_external_source(source_type) -> bool:
    return str(getattr(source_type, "value", source_type)).lower() in ("mysql", "postgresql")


def sql_literal(value: str) -> str:
    """SQL 字符串字面量：单引号翻倍。"""
    return "'" + str(value).replace("'", "''") + "'"


def sql_ident(value: str) -> str:
    """SQL 标识符：双引号翻倍。"""
    return '"' + str(value).replace('"', '""') + '"'


def secret_name_for(schema_name: str) -> str:
    digest = hashlib.sha1(schema_name.encode("utf-8")).hexdigest()[:12]
    return f"lvco_ds_{digest}"


# TLS 模式映射：产品层统一用 libpq 的 sslmode 取值，MySQL 扩展另有自己的枚举
_PG_SSL_MODES = {"disable", "prefer", "require", "verify-ca", "verify-full"}
_MYSQL_SSL_MODES = {
    "disable": "disabled",
    "prefer": "preferred",
    "require": "required",
    "verify-ca": "verify_ca",
    "verify-full": "verify_identity",
}


def build_attach_statements(conn_info: dict, schema_name: str, source_type) -> list[str]:
    """生成 [CREATE SECRET, ATTACH] 两条语句。

    conn_info 支持 db_name/username，也兼容旧调用方的 database/user；
    可选 ssl_mode（disable/prefer/require/verify-ca/verify-full）与 ssl_ca_path
    （CA 证书文件路径，仅在 verify-* 模式下有意义）。
    """
    stype = str(getattr(source_type, "value", source_type)).lower()
    host = str(conn_info.get("host") or "localhost")
    try:
        port = int(conn_info.get("port") or default_port(stype))
    except (TypeError, ValueError):
        port = default_port(stype)
    user = str(conn_info.get("username") or conn_info.get("user") or "")
    database = str(conn_info.get("db_name") or conn_info.get("database") or "")
    password = str(conn_info.get("password") or "")
    secret = secret_name_for(schema_name)
    secret_type = "mysql" if stype == "mysql" else "postgres"

    fields = [
        f"TYPE {secret_type}",
        f"HOST {sql_literal(host)}",
        f"PORT {port}",
    ]
    if user:
        fields.append(f"USER {sql_literal(user)}")
    if password:
        fields.append(f"PASSWORD {sql_literal(password)}")
    if database:
        db_field = "DATABASE" if secret_type == "mysql" else "DBNAME"
        fields.append(f"{db_field} {sql_literal(database)}")

    # TLS：默认 prefer（服务器支持就加密），require 强制加密，verify-* 还要校验证书。
    # DuckDB 的 postgres/mysql 扩展都支持在 SECRET 里声明 TLS 参数（已实测）。
    ssl_mode = str(conn_info.get("ssl_mode") or "prefer").strip().lower()
    ssl_ca_path = str(conn_info.get("ssl_ca_path") or "").strip()
    if secret_type == "postgres":
        if ssl_mode not in _PG_SSL_MODES:
            ssl_mode = "prefer"
        fields.append(f"SSLMODE {sql_literal(ssl_mode)}")
        if ssl_ca_path and ssl_mode in ("verify-ca", "verify-full"):
            fields.append(f"SSLROOTCERT {sql_literal(ssl_ca_path)}")
    else:
        fields.append(f"SSL_MODE {sql_literal(_MYSQL_SSL_MODES.get(ssl_mode, 'preferred'))}")

    joined = ", ".join(fields)
    create_secret = f"CREATE OR REPLACE SECRET {secret} ({joined})"
    attach = (
        f"ATTACH '' AS {sql_ident(schema_name)} "
        f"(TYPE {secret_type}, SECRET {secret}, READ_ONLY)"
    )
    return [create_secret, attach]


def table_ref(schema_name: str, source_type, table_name: str = "", *, database: str = "") -> str:
    """数据源在 DuckDB 里的表引用（PG 三段式 / MySQL 三段式 / 本地 data 表）。"""
    stype = str(getattr(source_type, "value", source_type)).lower()
    table = table_name or "data"
    if stype == "postgresql":
        return f"{sql_ident(schema_name)}.public.{sql_ident(table)}"
    if stype == "mysql":
        # MySQL 扩展的 catalog 下只有一个默认库 schema，沿用两段式（历史行为）。
        return f"{sql_ident(schema_name)}.{sql_ident(table)}"
    return f"{sql_ident(schema_name)}.data"
