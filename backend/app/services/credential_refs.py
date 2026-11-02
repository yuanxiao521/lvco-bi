"""服务端凭据引用（让数据库密码不必经过浏览器）。

支持的引用
----------
- env:NNAME     从环境变量读（docker-compose / k8s secret 注入，最常见）
- file:/path    从文件读（docker secret / vault agent 落盘的场景）

设计约束（都是真机踩过或可预见的攻击面）
----------------------------------------
1. 只允许这两种 scheme，未知 scheme 明确报错而不是"静默当密码用"；
2. env 引用禁止指到应用自身密钥（JWT_SECRET_KEY / OPENAI_API_KEY / DB_ENCRYPTION_KEY …），
   否则用户可以借"数据库密码"通道把系统密钥喂给驱动/日志；
3. 可用 DB_SECRET_ENV_ALLOWLIST 把 env 收口到白名单（生产建议开启）；
4. file 引用必须落在 DB_SECRET_FILE_ROOTS 之内（默认 /run/secrets 与 ./data/secrets），
   并先 resolve 再判断，防 ../ 与符号链接越权；
5. 引用解析结果只进内存，不落库、不回传、不写日志（日志只打 scheme+标识）。
"""
import os
import pathlib
import re

from app.config import settings

SCHEMES = ("env", "file")

# 应用自身密钥：绝不允许被当作数据库凭据引用
_DENY_ENV = {
    "JWT_SECRET_KEY",
    "DB_ENCRYPTION_KEY",
    "OPENAI_API_KEY",
    "LANGFUSE_SECRET_KEY",
    "MINIO_SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
}

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class CredentialRefError(ValueError):
    """凭据引用不合法/无法解析（message 可直接展示给用户）。"""


def parse_ref(ref: str) -> tuple[str, str]:
    raw = (ref or "").strip()
    if not raw:
        raise CredentialRefError("凭据引用不能为空")
    if ":" not in raw:
        raise CredentialRefError(
            "凭据引用格式应为 env:变量名 或 file:/绝对路径（例如 env:LVCO_PG_PASSWORD）"
        )
    scheme, target = raw.split(":", 1)
    scheme = scheme.strip().lower()
    target = target.strip()
    if scheme not in SCHEMES:
        raise CredentialRefError(
            f"不支持的凭据来源 {scheme!r}，目前只支持：" + "、".join(SCHEMES)
        )
    if not target:
        raise CredentialRefError(f"凭据引用 {scheme}: 后面缺少目标")
    return scheme, target


def _check_env_name(name: str) -> str:
    if not _ENV_NAME_RE.match(name):
        raise CredentialRefError("环境变量名不合法（只允许字母、数字、下划线，且不能以数字开头）")
    if name.upper() in _DENY_ENV:
        raise CredentialRefError(
            f"不允许引用应用自身的密钥变量 {name}（会被当作数据库密码使用）"
        )
    allowlist = [
        item.strip()
        for item in (settings.DB_SECRET_ENV_ALLOWLIST or "").split(",")
        if item.strip()
    ]
    if allowlist and name not in allowlist:
        raise CredentialRefError(
            "该环境变量不在 DB_SECRET_ENV_ALLOWLIST 白名单内，请联系管理员添加"
        )
    return name


def _allowed_file_roots() -> list[pathlib.Path]:
    roots = []
    for item in (settings.DB_SECRET_FILE_ROOTS or "").split(","):
        item = item.strip()
        if item:
            roots.append(pathlib.Path(item).expanduser().resolve())
    return roots


def _check_file_path(target: str) -> pathlib.Path:
    path = pathlib.Path(target).expanduser()
    if not path.is_absolute():
        raise CredentialRefError("file: 引用必须是绝对路径")
    resolved = path.resolve()
    roots = _allowed_file_roots()
    if not roots:
        raise CredentialRefError(
            "未配置 DB_SECRET_FILE_ROOTS，file: 引用被禁用；请改用 env: 或配置允许目录"
        )
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise CredentialRefError(
            "凭据文件必须位于允许目录内（DB_SECRET_FILE_ROOTS）："
            + "、".join(str(r) for r in roots)
        )
    return resolved


def validate_ref(ref: str) -> str:
    """只做格式/范围校验，不要求目标此刻存在。返回规范化后的引用。"""
    scheme, target = parse_ref(ref)
    if scheme == "env":
        _check_env_name(target)
        return f"env:{target}"
    _check_file_path(target)
    return f"file:{target}"


def resolve_ref(ref: str) -> str:
    """解析出明文凭据。目标不存在时给出可操作报错（而不是空密码连库）。"""
    scheme, target = parse_ref(ref)
    if scheme == "env":
        name = _check_env_name(target)
        value = os.environ.get(name)
        if not value:
            raise CredentialRefError(
                f"环境变量 {name} 未设置或为空：请在部署侧注入该变量后重试",
            )
        return value
    path = _check_file_path(target)
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except FileNotFoundError as exc:
        raise CredentialRefError(f"凭据文件不存在：{path}") from exc
    except OSError as exc:
        raise CredentialRefError(f"凭据文件读取失败：{path}（{exc.strerror or exc}）") from exc
    if not value:
        raise CredentialRefError(f"凭据文件为空：{path}")
    return value


def describe_ref(ref: str | None) -> str:
    """日志/前端展示用的安全形态（引用标识本身不是密钥）。"""
    if not ref:
        return ""
    try:
        scheme, target = parse_ref(ref)
    except CredentialRefError:
        return "invalid"
    return f"{scheme}:{target}"
