from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.security import hash_password, verify_password
from app.models.user import User
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


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)) -> SuccessResponse:
    service = AuthService(db)
    user = await service.authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "邮箱或密码错误"},
        )
    tokens = service.create_tokens(user)
    return SuccessResponse(
        data={
            **tokens,
            "user": UserResponse.model_validate(user).model_dump(mode="json", by_alias=True),
        }
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> SuccessResponse:
    service = AuthService(db)
    try:
        user = await service.register(body.email, body.password, body.display_name)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "CONFLICT", "message": str(e)},
        )
    tokens = service.create_tokens(user)
    return SuccessResponse(
        data={
            **tokens,
            "user": UserResponse.model_validate(user).model_dump(mode="json", by_alias=True),
        }
    )


@router.post("/refresh")
async def refresh_token(body: RefreshTokenRequest, db: AsyncSession = Depends(get_db)) -> SuccessResponse:
    service = AuthService(db)
    new_access_token = service.refresh_access_token(body.refresh_token)
    if new_access_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Refresh Token 无效或已过期"},
        )
    return SuccessResponse(data={"accessToken": new_access_token, "tokenType": "bearer"})


@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    await db.commit()

    return SuccessResponse(data={"message": "密码修改成功"})


@router.patch("/profile")
async def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新个人资料"""
    from sqlalchemy import select

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
