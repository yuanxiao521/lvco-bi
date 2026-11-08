from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import assert_token_state, get_current_user
from app.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.repositories import SQLAlchemyUserRepository
from app.schemas import (
    CamelModel,
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    SuccessResponse,
    UserResponse,
)
from app.services.auth_service import AuthService


class ChangePasswordRequest(CamelModel):
    old_password: str
    new_password: str


class UpdateProfileRequest(CamelModel):
    display_name: str | None = None
    email: str | None = None


router = APIRouter(prefix="/auth", tags=["认证"])


def _user_token_version(user: User) -> int:
    """读用户的当前令牌版本（None 视为 0，兼容迁移前的内存对象/旧行）。"""
    return int(getattr(user, "token_version", 0) or 0)


def _issue_tokens(user: User) -> dict:
    """签发 access + refresh 双令牌，并把**当前** token_version 写进 claim。

    为什么这里不复用 `AuthService.create_tokens`：那个方法不读 `user.token_version`
    （AuthService 不在本次改动的文件范围内）。如果登录继续签发 version=0 的令牌，
    用户一旦登出（DB version 变成 1），之后每次登录拿到的令牌都会立刻被 deps 判 401
    —— 账号被永久锁死。因此令牌签发必须与"读版本号"绑定，见 docs 里的说明。
    """
    version = _user_token_version(user)
    return {
        "accessToken": create_access_token(
            str(user.id), {"email": user.email}, token_version=version
        ),
        "refreshToken": create_refresh_token(str(user.id), token_version=version),
        "tokenType": "bearer",
        "expiresIn": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": message},
    )


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)) -> SuccessResponse:
    user_repo = SQLAlchemyUserRepository(db)
    service = AuthService(user_repo)
    user = await service.authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "邮箱或密码错误"},
        )
    tokens = _issue_tokens(user)
    return SuccessResponse(
        data={
            **tokens,
            "user": UserResponse.model_validate(user).model_dump(mode="json", by_alias=True),
        }
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> SuccessResponse:
    user_repo = SQLAlchemyUserRepository(db)
    service = AuthService(user_repo)
    try:
        user = await service.register(body.email, body.password, body.display_name)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CONFLICT", "message": str(e)},
        )
    tokens = _issue_tokens(user)
    return SuccessResponse(
        data={
            **tokens,
            "user": UserResponse.model_validate(user).model_dump(mode="json", by_alias=True),
        }
    )


@router.post("/refresh")
async def refresh_token(body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)) -> SuccessResponse:
    """用 refresh token 换新的 access token。

    **必须在这里也做吊销校验**：refresh 有效期 7 天，远长于 access 的 30 分钟。
    如果只校验 access token，攻击者拿着一次盗取的 refresh token 就能在登出之后
    继续换新 access token，P0 止血等于没做（原实现还漏了"用户是否仍存在"）。
    """
    payload = decode_token(body.refresh_token)
    if payload is None or payload.get("type") != "refresh":
        raise _unauthorized("Refresh Token 无效或已过期")
    raw_uid = payload.get("sub")
    if not raw_uid:
        raise _unauthorized("Refresh Token 无效或已过期")
    try:
        user_uuid = UUID(str(raw_uid))
    except (ValueError, TypeError):
        raise _unauthorized("Refresh Token 无效或已过期")
    user = (
        await db.execute(select(User).where(User.id == user_uuid))
    ).scalar_one_or_none()
    if user is None:
        raise _unauthorized("用户不存在")
    # 账号停用 → 403；令牌已被登出/改密吊销 → 401
    assert_token_state(payload, user)
    new_access_token = create_access_token(
        str(user.id), {"email": user.email}, token_version=_user_token_version(user)
    )
    return SuccessResponse(data={"accessToken": new_access_token, "tokenType": "bearer"})


@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SuccessResponse:
    """登出：把该用户的 token_version +1，**立刻作废其全部已签发令牌**。

    为什么不是"把当前这一枚 token 拉黑"：JWT 是无状态的，服务端没有已签发令牌清单；
    版本号是成本最低的止血手段——一次写库换掉该账号所有 access + refresh 令牌。
    代价是"登出即全端下线"（多设备场景需重新登录），这对"离职/被盗"场景正是想要的语义。
    """
    current_user.token_version = _user_token_version(current_user) + 1
    await db.commit()
    return SuccessResponse(data={"message": "已成功登出"})


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改密码"""
    if not verify_password(body.old_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail={"code": "WRONG_PASSWORD", "message": "旧密码不正确"})

    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail={"code": "WEAK_PASSWORD", "message": "新密码长度不能少于 8 位"})

    current_user.password_hash = hash_password(body.new_password)
    # P0：改密同样要吊销该账号的全部旧令牌（否则被盗 token 在改密后仍然可用，
    # 而"改密"恰恰是用户发现异常后的第一反应）。
    current_user.token_version = _user_token_version(current_user) + 1
    await db.commit()

    # 旧令牌此刻已全部失效（包括发起本次请求的这一枚），因此直接把新令牌一并发回，
    # 让当前设备无感续期。前端需要取用 data.accessToken / data.refreshToken
    # （字段名与 /login 一致）；若前端未取用，用户会在下一次请求被 401 → 重新登录。
    new_tokens = _issue_tokens(current_user)
    return SuccessResponse(data={"message": "密码修改成功", **new_tokens})


@router.patch("/profile")
async def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新个人资料"""
    if body.display_name is not None:
        current_user.display_name = body.display_name

    if body.email is not None:
        # Check email uniqueness
        result = await db.execute(
            select(User).where(User.email == body.email, User.id != current_user.id)
        )
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail={"code": "EMAIL_TAKEN", "message": "该邮箱已被使用"})
        current_user.email = body.email

    await db.commit()
    await db.refresh(current_user)

    return SuccessResponse(data={
        "id": str(current_user.id),
        "email": current_user.email,
        "display_name": current_user.display_name,
    })
