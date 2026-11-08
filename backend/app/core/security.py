from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings


def hash_password(password: str) -> str:
    pwd = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pwd, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        pwd = plain_password.encode("utf-8")[:72]
        return bcrypt.checkpw(pwd, hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str,
    extra_claims: dict | None = None,
    *,
    token_version: int = 0,
) -> str:
    """签发 access token。

    token_version 会作为 claim 写入，与 users.token_version 比对（见 api/deps.py）。
    默认 0：**升级前签发的旧 token 没有该 claim，同样按 0 处理**——所以打开这项校验
    不会把存量用户一次性踢下线；只有发生过登出/改密（DB 版本被 +1）的账号才会 401。
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    claims: dict = {
        "sub": subject,
        "exp": expire,
        "type": "access",
        "token_version": int(token_version or 0),
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str, *, token_version: int = 0) -> str:
    """签发 refresh token（同样带 token_version）。

    必须带：否则登出后旧的 refresh token 仍能换出新 access token，
    吊销等于没做（refresh 有效期 7 天，远长于 access 的 30 分钟）。
    """
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    claims: dict = {
        "sub": subject,
        "exp": expire,
        "type": "refresh",
        "token_version": int(token_version or 0),
    }
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def token_version_of(payload: dict | None) -> int:
    """读 payload 里的 token_version。

    缺省 / 非法一律返回 0（= 兼容升级前签发的旧 token，不让存量用户重新登录）。
    """
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("token_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def decode_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None
