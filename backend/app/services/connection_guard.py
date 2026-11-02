"""外部数据库连接的输入与网络边界校验（连接前的第一道闸）。

为什么需要这一层
----------------
1. host / port / db / user / table 会被拼进 DuckDB 的 SECRET / ATTACH 语句。
   只要放行引号、空格、分号、注释符，就等于把"用户输入"当 SQL 执行；
2. 数据源连接本质是"让服务器替用户发起 TCP 连接"，属于典型 SSRF 面：
   攻击者可以把 host 指向云厂商元数据服务（169.254.169.254）、本机回环端口
   或内网管理面，用返回结果/耗时做端口扫描；
3. 产品现实：智能 BI 的常见用法就是连**内网**数据库，所以不能一刀切禁内网。
   策略因此是"默认允许私网 + 硬禁元数据/组播/0.0.0.0 + 可选白名单收口"。

配置项（backend/.env）
----------------------
- DB_ALLOW_PRIVATE_HOSTS：是否允许连私网/回环（默认 true，本地与内网部署需要）；
- DB_ALLOWED_HOSTS：逗号分隔的白名单（填了以后只允许这些 host 或其后缀，
  用于把线上多租户环境收口到"已知数据源"）；
- DB_CONNECT_TIMEOUT_SECONDS：建连超时（下游连接器使用）。
"""
import ipaddress
import re

from app.config import settings

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_TABLE_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")

# 云厂商元数据/链路本地地址：无论如何都不允许（SSRF 最高危目标）
_DENY_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),    # AWS/GCP/Azure 元数据（169.254.169.254）
    ipaddress.ip_network("100.100.100.200/32"),  # 阿里云元数据
    ipaddress.ip_network("fd00:ec2::254/128"),   # AWS IPv6 元数据
]
_DENY_HOSTS = {"0.0.0.0", "255.255.255.255", "metadata", "metadata.google.internal", "[::]"}


class ConnectionValidationError(ValueError):
    """连接参数不合法（可直接展示给用户的中文消息）。"""


def _require_text(value, field: str, max_len: int = 200) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ConnectionValidationError(f"{field} 不能为空")
    if len(text) > max_len:
        raise ConnectionValidationError(f"{field} 过长（最多 {max_len} 个字符）")
    return text


def _is_denied_ip(ip: ipaddress._BaseAddress) -> bool:
    if any(ip in net for net in _DENY_NETWORKS):
        return True
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    return False


def validate_host(host: str) -> str:
    """校验数据库主机名/IP。返回规范化后的 host。"""
    host = _require_text(host, "主机地址", 253)
    lowered = host.lower()
    if lowered in _DENY_HOSTS:
        raise ConnectionValidationError(f"不允许连接主机 {host}（保留地址）")
    if any(ch in host for ch in (" ", "/", "\\", "\t", "\n", "\r", ";", "@")):
        raise ConnectionValidationError("主机地址含有非法字符（不允许空格、斜杠、分号、@ 等）")
    if "://" in host:
        raise ConnectionValidationError("主机地址不要带协议前缀（如 postgres://）")

    ip = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _is_denied_ip(ip):
            raise ConnectionValidationError(
                f"不允许连接 {host}：链路本地/保留地址（云元数据等）不在允许范围"
            )
        if (ip.is_private or ip.is_loopback) and not settings.DB_ALLOW_PRIVATE_HOSTS:
            raise ConnectionValidationError(
                f"不允许连接内网地址 {host}（当前 DB_ALLOW_PRIVATE_HOSTS=false）"
            )
    else:
        if not _HOSTNAME_RE.match(host):
            raise ConnectionValidationError("主机地址格式不合法（只允许字母、数字、点、中划线、下划线）")

    allowlist = [h.strip().lower() for h in (settings.DB_ALLOWED_HOSTS or "").split(",") if h.strip()]
    if allowlist:
        ok = any(lowered == a or lowered.endswith("." + a) for a in allowlist)
        if not ok:
            raise ConnectionValidationError(
                "该主机不在 DB_ALLOWED_HOSTS 白名单内，请联系管理员添加"
            )
    return host


def validate_port(port, default: int) -> int:
    if port in (None, ""):
        return default
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise ConnectionValidationError("端口必须是数字") from None
    if not (1 <= value <= 65535):
        raise ConnectionValidationError("端口必须在 1-65535 之间")
    return value


def validate_name(value, field: str, *, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise ConnectionValidationError(f"{field} 不能为空")
        return ""
    if not _NAME_RE.match(text):
        raise ConnectionValidationError(f"{field} 含有控制字符或长度超限")
    return text


def validate_table_name(value) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    if not _TABLE_RE.match(text):
        raise ConnectionValidationError("表名含有控制字符或长度超限")
    return text


def validate_connection_input(
    *, host, port, user, database, table_name="", source_type: str = "postgresql"
) -> dict:
    """一站式校验，返回规范化后的连接参数（不含密码）。"""
    default_port = 3306 if source_type == "mysql" else 5432
    return {
        "host": validate_host(host),
        "port": validate_port(port, default_port),
        "user": validate_name(user, "用户名"),
        "database": validate_name(database, "数据库名"),
        "table_name": validate_table_name(table_name),
    }
