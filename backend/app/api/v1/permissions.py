"""权限管理 API：轻量版，仅暴露用户列表 + admin 修改角色。"""
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User, UserRole
from app.schemas import CamelModel, SuccessResponse, UserResponse

router = APIRouter(prefix="/permissions", tags=["权限管理"])


class RoleUpdateRequest(CamelModel):
    role: UserRole


class UserListItem(CamelModel):
    id: UUID
    email: str
    display_name: str
    role: UserRole
    avatar_url: str | None
    created_at: str
    last_login_at: str | None = None
    # 资源计数（轻量附加）
    datasource_count: int = 0
    canvas_count: int = 0
    dashboard_count: int = 0


class UserListResponse(CamelModel):
    items: list[UserListItem]
    total: int
    page: int
    page_size: int
    pages: int


@router.get("/users")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = None,
    role: UserRole | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """列出所有用户（任何登录用户可看，admin/editor/viewer 都行）。"""
    from app.models.dashboard import Dashboard
    from app.models.datasource import DataSource
    from app.models.canvas import Canvas

    q = select(User)
    if search:
        like = f"%{search}%"
        q = q.where(or_(User.email.ilike(like), User.display_name.ilike(like)))
    if role is not None:
        q = q.where(User.role == role)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    q = q.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    users = (await db.execute(q)).scalars().all()

    user_ids = [u.id for u in users]

    # 批量统计资源数
    ds_counts: dict = {}
    canvas_counts: dict = {}
    dash_counts: dict = {}
    if user_ids:
        ds_rows = (await db.execute(
            select(DataSource.user_id, func.count(DataSource.id))
            .where(DataSource.user_id.in_(user_ids))
            .group_by(DataSource.user_id)
        )).all()
        ds_counts = {uid: cnt for uid, cnt in ds_rows}

        canvas_rows = (await db.execute(
            select(Canvas.user_id, func.count(Canvas.id))
            .where(Canvas.user_id.in_(user_ids))
            .group_by(Canvas.user_id)
        )).all()
        canvas_counts = {uid: cnt for uid, cnt in canvas_rows}

        dash_rows = (await db.execute(
            select(Dashboard.user_id, func.count(Dashboard.id))
            .where(Dashboard.user_id.in_(user_ids))
            .group_by(Dashboard.user_id)
        )).all()
        dash_counts = {uid: cnt for uid, cnt in dash_rows}

    items = [
        UserListItem(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            avatar_url=u.avatar_url,
            created_at=u.created_at.isoformat() if u.created_at else "",
            datasource_count=ds_counts.get(u.id, 0),
            canvas_count=canvas_counts.get(u.id, 0),
            dashboard_count=dash_counts.get(u.id, 0),
        )
        for u in users
    ]
    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(data=UserListResponse(
        items=items, total=total, page=page, page_size=page_size, pages=pages,
    ).model_dump(mode="json", by_alias=True))


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: UUID,
    body: RoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """仅 admin 可修改其他用户角色。"""
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "仅管理员可调整角色"},
        )
    # 防呆：禁止 admin 把自己降级（避免最后一名 admin 锁死系统）
    if user_id == current_user.id and body.role != UserRole.admin:
        # 检查是否还有别的 admin
        other_admin = (await db.execute(
            select(func.count()).select_from(User).where(
                User.role == UserRole.admin, User.id != current_user.id
            )
        )).scalar() or 0
        if other_admin == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "LAST_ADMIN", "message": "系统中需要至少保留一名管理员"},
            )

    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "用户不存在"},
        )
    target.role = body.role
    await db.commit()
    await db.refresh(target)
    return SuccessResponse(data=UserResponse.model_validate(target).model_dump(mode="json", by_alias=True))
