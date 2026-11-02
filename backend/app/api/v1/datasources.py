import logging
import math
from uuid import UUID

_log = logging.getLogger("lvco.datasources")

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_datasource_service
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.datasource import DatasourceStatus, SourceType
from app.models.user import User
from app.schemas import CamelModel, DataSourceListResponse, DataSourceResponse, DataSourceUpdate, SuccessResponse
from app.config import settings
from app.services.ai_service import AIService
from app.services.connection_guard import ConnectionValidationError, validate_connection_input
from app.services.datasource_connection import (
    DatasourceConnectionError,
    credentials_for,
    ensure_attached,
    resolve_credentials,
    sanitize_error,
    table_ref_for,
)
from app.utils.crypto import CredentialKeyMissingError
from app.core.duckdb_client import duckdb_client
from app.services.data_quality import dup_row_count, format_issue_count, null_count, outlier_count, outlier_iqr_count, type_inconsistency_count
from app.services.datasource_service import DataSourceService
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient
from app.connectors.mysql_connector import mysql_connector
from app.connectors.postgres_connector import postgres_connector

router = APIRouter(prefix="/datasources", tags=["数据源"])


class DataSourceConnectBody(CamelModel):
    """创建一个连接 + N 张表。

    tableNames 是主入口（一次提交多张表，凭据只存一份）；tableName 保留为单表旧形态。
    sslMode/sslCaPem 是连接级 TLS 配置。
    """

    name: str | None = Field(None, max_length=200)
    source_type: SourceType
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    username: str | None = None
    password: str | None = None
    table_name: str | None = None
    table_names: list[str] | None = None
    ssl_mode: str | None = None
    ssl_ca_pem: str | None = None
    # manual = 前端输入密码；reference = 部署侧凭据引用（env:/file:），密码不经前端
    credential_source: str | None = None
    credential_ref: str | None = None


class TestConnectionRequest(CamelModel):
    source_type: str  # "mysql" | "postgresql"
    connection_info: dict  # {host, port, user, password, database}
    # 引用型凭据：前端不持有密码，由后端按引用解析后探测
    credential_ref: str | None = None
    credential_source: str | None = None


@router.get("")
async def list_datasources(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    type: SourceType | None = None,
    status_filter: DatasourceStatus | None = Query(None, alias="status"),
    search: str | None = None,
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        ds = await service.upload_file(current_user.id, name, file)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_REQUEST", "message": str(e)},
        )
    return SuccessResponse(data=DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True))


@router.post("/connect", status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
async def connect_datasource(
    request: Request,
    body: DataSourceConnectBody,
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """创建数据库型数据源。

    凭据处理链路：入参校验（connection_guard）→ AES-256-GCM 加密（crypto.seal_secret）
    → 落库；未配置 DB_ENCRYPTION_KEY 时拒绝创建（fail-closed），绝不明文存储。
    """
    try:
        connection, datasources = await service.create_connection(
            current_user.id, body.model_dump()
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
    _log.info(
        "datasource_created source_type=%s host=%s port=%s db=%s user=%s tables=%s tls=%s by=%s",
        body.source_type.value,
        body.host,
        body.port,
        body.db_name,
        body.username,
        len(datasources),
        connection.ssl_mode,
        current_user.id,
    )
    from app.schemas import ConnectDatasourceResponse, ConnectionResponse

    payload = ConnectDatasourceResponse(
        connection=ConnectionResponse.from_connection(connection, len(datasources)),
        datasources=[
            DataSourceResponse.model_validate(ds) for ds in datasources
        ],
    )
    return SuccessResponse(data=payload.model_dump(mode="json", by_alias=True))


@router.post("/{datasource_id}/disconnect")
async def disconnect_datasource(
    datasource_id: UUID,
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
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
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """列出 PostgreSQL 数据源的所有 public 表（psycopg2 直连，无 DuckDB 串库风险）"""
    ds = await service.get_by_id(datasource_id, current_user.id)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    if ds.source_type != SourceType.postgresql:
        return SuccessResponse(data={"tables": []})

    try:
        conn_info = credentials_for(ds)
    except DatasourceConnectionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "CREDENTIAL_UNAVAILABLE", "message": str(e)},
        ) from e

    try:
        tables = await postgres_connector.list_tables_direct(conn_info)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "CONNECT_FAILED",
                "message": sanitize_error(str(e), secrets=[conn_info.get("password")], limit=200),
            },
        ) from e
    return SuccessResponse(data={"tables": tables})


@router.get("/{datasource_id}/health")
async def datasource_health(
    datasource_id: UUID,
    service: DataSourceService = Depends(get_datasource_service),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """单个数据源视角的健康检查（内部按所属连接探测一次）。

    文件型数据源返回 status=None（健康检查只对数据库连接有意义）。
    """
    ds = await service.get_by_id(datasource_id, current_user.id)
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )
    if ds.connection_id is None:
        return SuccessResponse(
            data={"status": None, "error": "非数据库数据源，无需健康检查", "checkedAt": None}
        )
    from app.services.connection_service import ConnectionService

    payload = await ConnectionService(db).check_health(ds.connection_id, current_user.id, force=True)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "连接不存在"},
        )
    await db.commit()
    return SuccessResponse(
        data={
            "status": payload["status"],
            "error": payload["error"],
            "checkedAt": payload["checkedAt"].isoformat() if payload["checkedAt"] else None,
        }
    )


@router.post("/{datasource_id}/ai-clean")
async def ai_clean_datasource(
    datasource_id: UUID,
    check_types: str | None = Query(default=None, description="逗号分隔的检测类别，如 missing,outlier,duplicate。为空则检测全部"),
    db: AsyncSession = Depends(get_db),
    service: DataSourceService = Depends(get_datasource_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    import traceback as _tb
    allowed = None
    if check_types:
        allowed = set(t.strip().lower() for t in check_types.split(",") if t.strip())
    try:
        return await _do_ai_clean(datasource_id, db, service, current_user, allowed)
    except Exception as _exc:
        _log.error("ai_clean failed: %s\n%s", _exc, _tb.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "CLEAN_FAILED", "message": f"数据清洗分析失败: {_exc}"},
        )


async def _do_ai_clean(
    datasource_id: UUID,
    db: AsyncSession,
    service: DataSourceService,
    current_user: User,
    allowed_check_types: set[str] | None = None,
) -> SuccessResponse:
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

    # 如果指定了 check_types，只检测指定的类别
    run_missing = allowed_check_types is None or "missing" in allowed_check_types
    run_outlier = allowed_check_types is None or "outlier" in allowed_check_types
    run_outlier_iqr = allowed_check_types is None or "outlier_iqr" in allowed_check_types
    run_format = allowed_check_types is None or "format" in allowed_check_types
    run_type_mismatch = allowed_check_types is None or "type_mismatch" in allowed_check_types
    run_duplicate = allowed_check_types is None or "duplicate" in allowed_check_types

    for field in target_fields:
        name = field.get("name")
        category = field.get("category", "")
        if not name:
            continue

        if run_missing:
            try:
                null_result = await null_count(datasource_id=datasource_id, field=name, db=db)
                if null_result.get("count", 0) > 0:
                    duckdb_stats.append(null_result)
            except Exception as e:
                _log.warning("清洗分析 null_count 失败 field=%s: %s", name, e)

        if category == "measure":
            if run_outlier:
                try:
                    outlier_result = await outlier_count(datasource_id=datasource_id, field=name, db=db)
                    if outlier_result.get("count", 0) > 0:
                        duckdb_stats.append(outlier_result)
                except Exception as e:
                    _log.warning("清洗分析 outlier_count 失败 field=%s: %s", name, e)

            if run_outlier_iqr:
                try:
                    iqr_result = await outlier_iqr_count(datasource_id=datasource_id, field=name, db=db)
                    if iqr_result.get("count", 0) > 0:
                        duckdb_stats.append(iqr_result)
                except Exception as e:
                    _log.warning("清洗分析 outlier_iqr_count 失败 field=%s: %s", name, e)

        elif category == "dimension":
            if run_format:
                try:
                    fmt_result = await format_issue_count(datasource_id=datasource_id, field=name, db=db)
                    if fmt_result.get("count", 0) > 0:
                        duckdb_stats.append(fmt_result)
                except Exception as e:
                    _log.warning("清洗分析 format_issue_count 失败 field=%s: %s", name, e)

        # 所有字段做类型不一致检查
        if run_type_mismatch:
            try:
                type_result = await type_inconsistency_count(datasource_id=datasource_id, field=name, db=db)
                if type_result.get("count", 0) > 0:
                    duckdb_stats.append(type_result)
            except Exception as e:
                _log.warning("清洗分析 type_inconsistency_count 失败 field=%s: %s", name, e)

    if run_duplicate:
        try:
            dup_result = await dup_row_count(datasource_id=datasource_id, db=db)
            if dup_result.get("count", 0) > 0:
                duckdb_stats.append(dup_result)
        except Exception as e:
            _log.warning("清洗分析 dup_row_count 失败: %s", e)

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
    except Exception as e:
        _log.warning("清洗分析 LLM clean_suggest 意外异常，使用规则兜底: %s", e)
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
@limiter.limit("20/minute")
async def test_connection(
    request: Request,
    body: TestConnectionRequest,
    current_user = Depends(get_current_user),
):
    """试探数据库连通性（不落库）。

    安全要点：
    - 入参先过 connection_guard（拒云元数据/保留地址/非法字符），避免被当成 SSRF 探测器；
    - 限流，防止用本接口做内网端口扫描；
    - 返回给前端的错误串统一脱敏（不含密码/连接串）。
    """
    source_type = (body.source_type or "").lower()
    if source_type not in ("mysql", "postgresql"):
        raise HTTPException(status_code=400, detail=f"Unsupported source type: {body.source_type}")

    info = dict(body.connection_info or {})
    try:
        checked = validate_connection_input(
            host=info.get("host"),
            port=info.get("port"),
            user=info.get("user") or info.get("username"),
            database=info.get("database") or info.get("db_name"),
            table_name=info.get("table_name") or "",
            source_type=source_type,
        )
    except ConnectionValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CONNECTION", "message": str(e)},
        ) from e

    password = info.get("password", "")
    ref = body.credential_ref or info.get("credential_ref")
    if ref:
        from app.services.credential_refs import CredentialRefError, resolve_ref

        try:
            password = resolve_ref(ref)
        except CredentialRefError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_CREDENTIAL_REF", "message": str(e)},
            ) from e

    safe_info = {
        "host": checked["host"],
        "port": checked["port"],
        "user": checked["user"],
        "password": password,
        "database": checked["database"],
    }
    connector = mysql_connector if source_type == "mysql" else postgres_connector
    result = await connector.test_connection(safe_info)
    _log.info(
        "datasource_test_connection source_type=%s host=%s port=%s db=%s success=%s by=%s",
        source_type, checked["host"], checked["port"], checked["database"],
        bool(result.get("success")), current_user.id,
    )
    return SuccessResponse(data=result)
