"""外部数据库连接的唯一入口（凭据解析 + DuckDB ATTACH）。

为什么要有这一层
----------------
改造前，"解密密码 → 拼 ATTACH 连接串 → ensure_attached" 这段逻辑在 10 个地方
各写了一遍（datasource_service ×2、query_engine、agent_tools、statistics、
ai.py ×3、insight_engine ×2）。后果是：

1. 行为漂移：statistics 里解密失败被 except: pass 吞掉，agent_tools 里默认用户名
   与 datasource_service 不一致，ai.py 里 MySQL 也拼成 public."table"；
2. 安全边界无法收口：密码直接拼进 ATTACH 连接串，而 DuckDB 会把这条连接串
   原样存进 catalog —— SELECT path FROM duckdb_databases() 就能读到明文密码；
3. 修一处漏九处：任何凭据策略调整（轮换、脱敏、超时）都要改十遍。

现在的做法
----------
- 凭据只在内存中解密；落库永远是 seal_secret() 产出的密文（见 app/utils/crypto.py）；
- ATTACH 一律走 DuckDB SECRET（见 app/connectors/attach_sql.py），密码不再进入
  ATTACH 连接串/catalog 元数据；
- 外部库一律 READ_ONLY 附加：即便 LLM 生成的 SQL 被社工绕过，引擎层也写不进去；
- 错误信息统一脱敏后再落 schema_meta / 返回前端。
"""
import os
import pathlib
import re

import structlog

from app.config import settings

from app.connectors.attach_sql import (
    build_attach_statements,
    default_port,
    is_external_source,
    secret_name_for,
    sql_ident,
    sql_literal,
    table_ref,
)
from app.core.duckdb_client import duckdb_client
from app.services.connection_guard import validate_connection_input, validate_ssl
from app.utils.crypto import (
    CredentialDecryptError,
    CredentialKeyMissingError,
    is_encrypted,
    open_secret,
    seal_secret,
)

logger = structlog.get_logger("datasource_connection")

PG_DEFAULT_PORT = default_port("postgresql")
MYSQL_DEFAULT_PORT = default_port("mysql")

_SECRET_PATTERNS = [
    re.compile(r"(password\s*=\s*)([^\s;)'\"]+)", re.IGNORECASE),
    re.compile(r"(pwd\s*=\s*)([^\s;)'\"]+)", re.IGNORECASE),
    re.compile(r"(?<=:)([^:@/\s]+)(?=@)", re.IGNORECASE),  # user:password@host
]


class DatasourceConnectionError(RuntimeError):
    """外部数据源连接失败（message 已脱敏，可直接展示给用户）。"""


def canonicalize(connection_config: dict | None, source_type) -> dict:
    """把各种历史命名的连接配置归一成一套键（不触碰密码明文）。

    兼容 host/port/db_name/database/dbName、username/user、table_name/tableName。
    """
    cfg = dict(connection_config or {})
    port = cfg.get("port")
    try:
        port = int(port) if port not in (None, "") else default_port(str(getattr(source_type, "value", source_type)))
    except (TypeError, ValueError):
        port = default_port(str(getattr(source_type, "value", source_type)))
    return {
        "host": str(cfg.get("host") or "localhost").strip(),
        "port": port,
        "db_name": str(cfg.get("db_name") or cfg.get("database") or cfg.get("dbName") or "").strip(),
        "username": str(cfg.get("username") or cfg.get("user") or "").strip(),
        "password": cfg.get("password") or "",
        "table_name": str(cfg.get("table_name") or cfg.get("tableName") or "").strip(),
        "ssl_mode": str(cfg.get("ssl_mode") or cfg.get("sslmode") or "").strip(),
    }


def resolve_credentials(connection_config: dict | None, source_type) -> dict:
    """归一 + 解密，返回可直接用于建连的配置（含明文密码）。

    只在内存里存在；调用方不得把它写日志、写库、回传前端。
    """
    conn = canonicalize(connection_config, source_type)
    raw = conn.get("password") or ""
    if raw:
        try:
            conn["password"] = open_secret(raw)
        except (CredentialDecryptError, CredentialKeyMissingError) as exc:
            raise DatasourceConnectionError(str(exc)) from exc
    return conn


def needs_secret_upgrade(connection_config: dict | None) -> bool:
    """历史明文 / 旧 CBC 密文需要升级为 v2 密文。"""
    pwd = (connection_config or {}).get("password")
    return bool(pwd) and not is_encrypted(pwd)


def seal_connection_config(
    *, host, port, db_name, username, password, table_name="", source_type="postgresql"
) -> dict:
    """校验 + 加密，产出可以安全落库的 connection_config。

    Raises:
        ConnectionValidationError: 参数不合法；
        CredentialKeyMissingError: 未配置 DB_ENCRYPTION_KEY（拒绝明文落库）。
    """
    checked = validate_connection_input(
        host=host,
        port=port,
        user=username,
        database=db_name,
        table_name=table_name,
        source_type=str(getattr(source_type, "value", source_type)),
    )
    sealed = seal_secret(password) if password else None
    stored = {
        "host": checked["host"],
        "port": checked["port"],
        "db_name": checked["database"],
        "username": checked["user"],
        "table_name": checked["table_name"],
        "secret_format": "v2",
    }
    if sealed:
        stored["password"] = sealed
    return stored


def ensure_attached(datasource, schema_name: str, *, force: bool = False) -> bool:
    """按数据源对象把外部库附加到 DuckDB（首次后复用）。

    Args:
        datasource: DataSource 对象（需要 source_type / connection_config）。
        schema_name: DuckDB 里使用的 catalog 名。
        force: True 时 DETACH 后重建（同步 / 密码轮换场景）。

    Returns:
        True 表示本次真的建了连接。
    """
    source_type = getattr(datasource, "source_type", None)
    if not is_external_source(source_type):
        return False
    conn_info = credentials_for(datasource)
    statements = build_attach_statements(conn_info, schema_name, source_type)
    try:
        return duckdb_client.ensure_attached(schema_name, statements, force=force)
    except Exception as exc:  # noqa: BLE001
        raise DatasourceConnectionError(
            sanitize_error(str(exc), secrets=[conn_info.get("password")])
        ) from exc


def attach_for_connection(conn_info: dict, schema_name: str, source_type) -> bool:
    """已解密连接配置直接 ATTACH（给不走 DataSource 对象的调用方）。"""
    statements = build_attach_statements(conn_info, schema_name, source_type)
    try:
        return duckdb_client.ensure_attached(schema_name, statements)
    except Exception as exc:  # noqa: BLE001
        raise DatasourceConnectionError(
            sanitize_error(str(exc), secrets=[conn_info.get("password")])
        ) from exc



# ── 连接（db_connections）侧的凭据处理 ───────────────────────────────

def build_connection_columns(
    *, host, port, db_name, username, password, ssl_mode="prefer",
    ssl_ca_pem=None, source_type="postgresql",
) -> dict:
    """校验 + 加密，产出可直接写 DBConnection 的列值。

    Raises:
        ConnectionValidationError: 参数/TLS 配置不合法；
        CredentialKeyMissingError: 未配置 DB_ENCRYPTION_KEY（拒绝明文落库）。
    """
    checked = validate_connection_input(
        host=host, port=port, user=username, database=db_name,
        table_name="", source_type=str(getattr(source_type, "value", source_type)),
    )
    mode, ca = validate_ssl(ssl_mode, ssl_ca_pem)
    return {
        "host": checked["host"],
        "port": checked["port"],
        "db_name": checked["database"],
        "username": checked["user"],
        "password_enc": seal_secret(password) if password else None,
        "ssl_mode": mode,
        "ssl_ca_enc": seal_secret(ca) if ca else None,
    }


def connection_credentials(connection) -> dict:
    """从连接行解出建连所需的完整配置（含明文密码，只在内存）。"""
    password = ""
    if getattr(connection, "password_enc", None):
        try:
            password = open_secret(connection.password_enc)
        except (CredentialDecryptError, CredentialKeyMissingError) as exc:
            raise DatasourceConnectionError(str(exc)) from exc
    ca_pem = ""
    if getattr(connection, "ssl_ca_enc", None):
        try:
            ca_pem = open_secret(connection.ssl_ca_enc)
        except (CredentialDecryptError, CredentialKeyMissingError) as exc:
            raise DatasourceConnectionError(str(exc)) from exc
    return {
        "host": connection.host,
        "port": connection.port,
        "db_name": connection.db_name or "",
        "username": connection.username or "",
        "password": password,
        "ssl_mode": getattr(connection, "ssl_mode", None) or "prefer",
        "ssl_ca_pem": ca_pem,
    }


def credentials_for(datasource) -> dict:
    """数据源的建连配置：优先走所属连接行，历史行回落到 connection_config。

    这样"凭据一份、多表共享"和"存量数据不动也能跑"同时成立。
    """
    cfg = dict(getattr(datasource, "connection_config", None) or {})
    connection = getattr(datasource, "connection", None)
    if connection is not None:
        creds = connection_credentials(connection)
        table_name = cfg.get("table_name") or ""
        if not table_name:
            meta = getattr(datasource, "schema_meta", None) or {}
            if isinstance(meta, dict):
                table_name = meta.get("table_name") or ""
        creds["table_name"] = table_name
        return creds
    return resolve_credentials(cfg, getattr(datasource, "source_type", None))


def materialize_ca(connection_id, pem: str | None) -> str | None:
    """把连接里存的 CA PEM 落到本地文件，供 DuckDB SSLROOTCERT 使用。

    CA 证书是公开材料（不是私钥），但仍是运行时产物：写入 data/tls/，权限收紧到 0600，
    内容变化时覆盖（轮换 CA 后下一次 ATTACH 生效）。
    """
    if not pem:
        return None
    base = pathlib.Path(getattr(settings, "TLS_CA_DIR", "./data/tls"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"ca_{connection_id}.pem"
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != pem:
            path.write_text(pem, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except OSError as exc:  # noqa: BLE001
        logger.warning("tls_ca_write_failed", connection_id=str(connection_id), error=str(exc)[:120])
        return None
    return str(path)


def attach_connection(connection, schema_name: str, *, force: bool = False) -> bool:
    """按连接行直接 ATTACH（健康检查/轮换后重建用，不依赖 DataSource 对象）。"""
    creds = connection_credentials(connection)
    ca_path = materialize_ca(connection.id, creds.get("ssl_ca_pem"))
    if ca_path:
        creds["ssl_ca_path"] = ca_path
    statements = build_attach_statements(creds, schema_name, connection.source_type)
    try:
        return duckdb_client.ensure_attached(schema_name, statements, force=force)
    except Exception as exc:  # noqa: BLE001
        raise DatasourceConnectionError(
            sanitize_error(str(exc), secrets=[creds.get("password")])
        ) from exc


def detach(schema_name: str) -> None:
    """从 DuckDB 摘掉一个 catalog（连接删除/重建时调用，失败忽略）。"""
    try:
        duckdb_client.execute(f'DETACH "{schema_name}"')
    except Exception:  # noqa: BLE001
        pass
    try:
        duckdb_client._attached_cache.discard(schema_name)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def connection_summary(connection) -> str:
    """日志/提示用的一句话摘要（绝不含凭据）。"""
    return (
        f"{connection.source_type}://{connection.username}@{connection.host}:"
        f"{connection.port}/{connection.db_name}"
    )

def table_ref_for(schema_name: str, source_type, table_name: str = "", *, database: str = "") -> str:
    """数据源在 DuckDB 里的表引用（带正确转义与 PG/MySQL 差异）。"""
    return table_ref(schema_name, source_type, table_name, database=database)


def sanitize_error(text: str, *, secrets: list[str] | None = None, limit: int = 300) -> str:
    """抹掉错误信息里的密码/连接串片段，再截断。"""
    if not text:
        return ""
    out = str(text)
    for token in secrets or []:
        if token and len(token) >= 3:
            out = out.replace(token, "***")
    out = _SECRET_PATTERNS[0].sub(r"\1***", out)
    out = _SECRET_PATTERNS[1].sub(r"\1***", out)
    out = _SECRET_PATTERNS[2].sub(r"***", out)
    return out[:limit]
