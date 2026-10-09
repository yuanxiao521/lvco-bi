"""AuthService：统一认证服务。

重构后使用 UserRepository 访问数据，自身只负责：
- 验证密码
- 创建/刷新 JWT
- 业务规则（邮箱已注册检查等）
"""
from __future__ import annotations

from app.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.repositories import SQLAlchemyUserRepository


class AuthService:
    """认证服务：登录、注册、令牌签发。"""

    def __init__(self, user_repo: SQLAlchemyUserRepository) -> None:
        self.user_repo = user_repo

    async def authenticate(self, email: str, password: str) -> User | None:
        """验证邮箱+密码，返回 User 或 None。"""
        user = await self.user_repo.get_by_email(email)
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    async def register(self, email: str, password: str, display_name: str) -> User:
        """注册新用户。邮箱已存在时抛 ValueError。"""
        existing = await self.user_repo.get_by_email(email)
        if existing is not None:
            raise ValueError("邮箱已被注册")
        return await self.user_repo.create(
            email=email,
            password_hash=hash_password(password),
            display_name=display_name,
        )

    async def update_profile(
        self,
        user_id: str,
        display_name: str | None = None,
        avatar_url: str | None = None,
    ) -> User | None:
        """更新用户资料。"""
        from uuid import UUID
        return await self.user_repo.update(
            UUID(user_id),
            display_name=display_name,
            avatar_url=avatar_url,
        )

    async def change_password(
        self,
        user_id: str,
        old_password: str,
        new_password: str,
    ) -> bool:
        """修改密码。验证旧密码后写入新密码。"""
        from uuid import UUID
        user = await self.user_repo.get_by_id(UUID(user_id))
        if user is None:
            return False
        if not verify_password(old_password, user.password_hash):
            return False
        await self.user_repo.update(
            UUID(user_id),
            password_hash=hash_password(new_password),
        )
        return True

    def create_tokens(self, user: User) -> dict:
        """生成 access + refresh 双令牌。"""
        extra_claims = {"email": user.email}
        access_token = create_access_token(str(user.id), extra_claims)
        refresh_token = create_refresh_token(str(user.id))
        return {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "tokenType": "bearer",
            "expiresIn": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }

    def refresh_access_token(self, refresh_token: str) -> str | None:
        """用 refresh token 换取新的 access token。"""
        payload = decode_token(refresh_token)
        if payload is None or payload.get("type") != "refresh":
            return None
        user_id = payload.get("sub")
        if user_id is None:
            return None
        return create_access_token(user_id)
