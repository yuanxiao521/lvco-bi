"""数据库连接 API：一份凭据 → 多张表。

管理动作全部在"连接"粒度上：
- POST /connections/{id}/rotate-secret  轮换密码（先验证再落库，影响该连接下所有表）
- POST /connections/{id}/health         健康探测（连接级一次，不按表重复）
- PATCH /connections/{id}               改 TLS / 名称（改完自动重建 ATTACH）
"""
import logging
from math import ceil
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter
from app.models.user import User
from app.schemas import ConnectionResponse, SuccessResponse
from app.services.connection_guard import ConnectionValidationError
from app.services.connection_service import ConnectionService, ConnectionServiceError
from app.utils.crypto import CredentialKeyMissingError

router = APIRouter(prefix="/connections", tags=["数据库连接"])
_log = logging.getLogger("lvco.connections")


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "NOT_FOUND", "message": "连接不存在"},
    )


@router.get("")
async def list_connections(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = ConnectionService(db)
    items, total = await service.list(current_user.id, page=page, page_size=page_size)
    return SuccessResponse(
        data={
            "items": [
                ConnectionResponse.from_connection(c, service.datasource_count(c)).model_dump(
                    mode="json", by_alias=True
                )
                for c in items
            ],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "pages": ceil(total / page_size) if total else 0,
        }
    )


@router.get("/{connection_id}")
async def get_connection(
    connection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = ConnectionService(db)
    connection = await service.get(connection_id, current_user.id)
    if connection is None:
        raise _not_found()
    payload = ConnectionResponse.from_connection(
        connection, service.datasource_count(connection)
    )
    return SuccessResponse(data=payload.model_dump(mode="json", by_alias=True))


@router.patch("/{connection_id}")
async def update_connection(
    connection_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = ConnectionService(db)
    try:
        connection = await service.update(
            connection_id,
            current_user.id,
            name=body.get("name"),
            ssl_mode=body.get("ssl_mode", body.get("sslMode")),
            ssl_ca_pem=body.get("ssl_ca_pem", body.get("sslCaPem")),
        )
    except ConnectionValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CONNECTION", "message": str(e)},
        ) from e
    except CredentialKeyMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ENCRYPTION_KEY_MISSING", "message": str(e)},
        ) from e
    if connection is None:
        raise _not_found()
    await db.commit()
    payload = ConnectionResponse.from_connection(
        connection, service.datasource_count(connection)
    )
    return SuccessResponse(data=payload.model_dump(mode="json", by_alias=True))


@router.post("/{connection_id}/rotate-secret")
@limiter.limit("10/minute")
async def rotate_secret(
    request: Request,
    connection_id: UUID,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """轮换数据库密码：先拿新密码探测，通过后才落库并对所有表重建 ATTACH。"""
    password = body.get("password")
    if password is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_REQUEST", "message": "请提供新密码"},
        )
    service = ConnectionService(db)
    try:
        result = await service.rotate_secret(connection_id, current_user.id, str(password))
    except ConnectionServiceError as e:
        raise _not_found() from e
    except ConnectionValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ROTATE_FAILED", "message": str(e)},
        ) from e
    except CredentialKeyMissingError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ENCRYPTION_KEY_MISSING", "message": str(e)},
        ) from e
    await db.commit()
    _log.info(
        "connection_secret_rotated connection_id=%s by=%s tables=%s",
        connection_id, current_user.id, len(result["datasources"]),
    )
    return SuccessResponse(
        data={
            "verified": True,
            "datasources": [
                {
                    "id": str(ds.id),
                    "name": ds.name,
                    "status": getattr(ds.status, "value", ds.status),
                    "rowCount": ds.row_count,
                }
                for ds in result["datasources"]
            ],
        }
    )


@router.post("/{connection_id}/health")
async def check_health(
    connection_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = ConnectionService(db)
    payload = await service.check_health(connection_id, current_user.id, force=True)
    if payload is None:
        raise _not_found()
    await db.commit()
    return SuccessResponse(
        data={
            "status": payload["status"],
            "error": payload["error"],
            "checkedAt": payload["checkedAt"].isoformat() if payload["checkedAt"] else None,
        }
    )
