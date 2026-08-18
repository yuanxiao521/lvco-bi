import math
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.datasource import DatasourceStatus, SourceType
from app.models.user import User
from app.schemas import CamelModel, DataSourceListResponse, DataSourceResponse, DataSourceUpdate, SuccessResponse
from app.config import settings
from app.services.ai_service import AIService
from app.utils.crypto import encrypt_value, get_encryption_key, decrypt_value
from app.core.duckdb_client import duckdb_client
from app.services.data_quality import dup_row_count, format_issue_count, null_count, outlier_count
from app.services.datasource_service import DataSourceService
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient
from app.connectors.mysql_connector import mysql_connector
from app.connectors.postgres_connector import postgres_connector

router = APIRouter(prefix="/datasources", tags=["数据源"])


class DataSourceConnectBody(CamelModel):
    name: str = Field(..., max_length=200)
    source_type: SourceType
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    username: str | None = None
    password: str | None = None
    table_name: str | None = None


class TestConnectionRequest(CamelModel):
    source_type: str  # "mysql" | "postgresql"
    connection_info: dict  # {host, port, user, password, database}


@router.get("")
async def list_datasources(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: SourceType | None = None,
    status_filter: DatasourceStatus | None = Query(None, alias="status"),
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    items, total = await service.list_datasources(
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        source_type=type,
        status=status_filter,
        search=search,
    )
    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(
        data=DataSourceListResponse(
            items=[DataSourceResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
        ).model_dump(mode="json", by_alias=True)
    )


@router.get("/{datasource_id}")
async def get_datasource(
    datasource_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    ds = await service.get_by_id(datasource_id, current_user.id)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.post("/upload", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def upload_datasource(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    try:
        ds = await service.upload_file(current_user.id, name, file)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_REQUEST", "message": str(e)},
        )
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.post("/connect", status_code=status.HTTP_201_CREATED)
async def connect_datasource(
    body: DataSourceConnectBody,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    body_dict = body.model_dump()
    if body.source_type in (SourceType.mysql, SourceType.postgresql) and body.password:
        key = get_encryption_key()
        if key:
            body_dict["password"] = encrypt_value(body.password, key)
    ds = await service.create_connection(current_user.id, body_dict)
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.post("/{datasource_id}/disconnect")
async def disconnect_datasource(
    datasource_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    ds = await service.update_status(datasource_id, current_user.id, DatasourceStatus.disconnected)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.post("/{datasource_id}/sync")
async def sync_datasource(
    datasource_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    ds = await service.sync_datasource(datasource_id, current_user.id)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.patch("/{datasource_id}/schema")
async def update_schema(
    datasource_id: UUID,
    body: DataSourceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    ds = await service.update_schema(datasource_id, current_user.id, body)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.delete("/{datasource_id}")
async def delete_datasource(
    datasource_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    deleted = await service.delete(datasource_id, current_user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return SuccessResponse(data={"message": "已删除"})


@router.get("/{datasource_id}/preview")
async def preview_datasource(
    datasource_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    data = await service.preview(datasource_id, current_user.id, limit)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    return SuccessResponse(data=data)


@router.get("/{datasource_id}/tables")
async def list_datasource_tables(
    datasource_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """列出 PostgreSQL 数据源的所有 public 表（用于洞察配置字段枚举）"""
    service = DataSourceService(db)
    ds = await service.get_by_id(datasource_id, current_user.id)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    if ds.source_type != SourceType.postgresql:
        return SuccessResponse(data={"tables": []})

    schema_name = duckdb_client.get_schema_name(current_user.id, datasource_id, ds.name)
    conn_info = dict(ds.connection_config) if ds.connection_config else {}
    key = get_encryption_key()
    if key and conn_info.get("password"):
        conn_info["password"] = decrypt_value(conn_info["password"], key)
    conn_info["user"] = conn_info.get("username", "postgres")
    conn_info["database"] = conn_info.get("db_name", "")

    try:
        duckdb_client.execute(f'DETACH "{schema_name}"')
    except Exception:
        pass
    attach_sql = postgres_connector.get_attach_sql(conn_info, schema_name)
    duckdb_client.execute(attach_sql)

    tables = postgres_connector.list_tables(duckdb_client, schema_name)
    return SuccessResponse(data={"tables": tables})


@router.post("/{datasource_id}/ai-clean")
async def ai_clean_datasource(
    datasource_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    service = DataSourceService(db)
    ds = await service.get_by_id(datasource_id, current_user.id)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    schema_meta = ds.schema_meta or {}
    all_fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
    target_fields = [f for f in all_fields if f.get("category") in ("measure", "dimension")][:10]

    duckdb_stats: list[dict] = []

    for field in target_fields:
        name = field.get("name")
        category = field.get("category", "")
        if not name:
            continue

        null_result = await null_count(datasource_id=datasource_id, field=name, db=db)
        if null_result.get("count", 0) > 0:
            duckdb_stats.append(null_result)

        if category == "measure":
            outlier_result = await outlier_count(datasource_id=datasource_id, field=name, db=db)
            if outlier_result.get("count", 0) > 0:
                duckdb_stats.append(outlier_result)

        elif category == "dimension":
            fmt_result = await format_issue_count(datasource_id=datasource_id, field=name, db=db)
            if fmt_result.get("count", 0) > 0:
                duckdb_stats.append(fmt_result)

    dup_result = await dup_row_count(datasource_id=datasource_id, db=db)
    if dup_result.get("count", 0) > 0:
        duckdb_stats.append(dup_result)

    if not duckdb_stats:
        return SuccessResponse(data={
            "summary": {"total_rows": 0},
            "issues": [],
        })

    field_meta = [{"name": f.get("name"), "category": f.get("category")} for f in target_fields if f.get("name")]

    try:
        ai_service = AIService(LLMClient(settings))
        issues = await ai_service.clean_suggest(field_meta, duckdb_stats)
    except (AINotConfiguredError, AIUpstreamError):
        from app.services.ai_service import _fallback_clean
        issues = _fallback_clean(duckdb_stats)

    stats_lookup: dict[tuple, dict] = {}
    for s in duckdb_stats:
        key = (s.get("field"), s.get("issue_type"))
        stats_lookup[key] = s

    for issue in issues:
        key = (issue.get("field"), issue.get("issue_type"))
        matched = stats_lookup.get(key)
        if matched:
            issue.setdefault("count", matched.get("count", 0))
            issue.setdefault("percentage", matched.get("percentage", 0.0))
            sample_raw = matched.get("sample_values")
            issue.setdefault("sample", sample_raw if isinstance(sample_raw, list) else [])
        else:
            issue.setdefault("count", 0)
            issue.setdefault("percentage", 0.0)
            issue.setdefault("sample", [])

    unique_problem_fields = {s.get("field") for s in duckdb_stats if s.get("field")}

    summary = {
        "total_columns": len(target_fields),
        "problem_columns": len(unique_problem_fields),
        "total_rows": ds.row_count or 0,
    }

    return SuccessResponse(data={"summary": summary, "issues": issues})


@router.post("/test-connection")
async def test_connection(
    body: TestConnectionRequest,
    current_user = Depends(get_current_user),
):
    """Test a database connection without saving."""
    if body.source_type == "mysql":
        result = await mysql_connector.test_connection(body.connection_info)
    elif body.source_type == "postgresql":
        result = await postgres_connector.test_connection(body.connection_info)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported source type: {body.source_type}")
    return SuccessResponse(data=result)
