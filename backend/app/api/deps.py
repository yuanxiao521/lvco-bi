from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User
from app.repositories.ai_session_repository import (
    SQLAlchemyAIMessageRepository,
    SQLAlchemyAISessionRepository,
)
from app.repositories.canvas_repository import (
    SQLAlchemyCanvasRepository,
    SQLAlchemyChartConfigRepository,
)
from app.repositories.dashboard_repository import (
    SQLAlchemyDashboardChartRepository,
    SQLAlchemyDashboardRepository,
)
from app.repositories.datasource_repository import SQLAlchemyDataSourceRepository
from app.repositories.datasource_schema_repository import (
    SQLAlchemyDataSourceSchemaRepository,
)
from app.repositories.fallback_cache import FallbackCacheRepository
from app.repositories.in_memory_cache import InMemoryCacheRepository
from app.repositories.notification_repository import SQLAlchemyNotificationRepository
from app.repositories.protocols import (
    CanvasRepository,
    ChartConfigRepository,
    DashboardChartRepository,
    DashboardRepository,
    DataSourceRepository,
    ReportRepository,
    UserPreferenceRepository,
)
from app.repositories.report_repository import SQLAlchemyReportRepository
from app.repositories.user_preference_repository import (
    SQLAlchemyUserPreferenceRepository,
)
from app.repositories.user_repository import SQLAlchemyUserRepository
from app.services.auth_service import AuthService
from app.services.canvas_service import CanvasService
from app.services.dashboard_service import DashboardService
from app.services.data_quality import DataQualityService
from app.services.datasource_service import DataSourceService
from app.services.notification_service import NotificationService
from app.services.report_service import ReportService
from app.services.user_preference_service import UserPreferenceService

security_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    payload = decode_token(token)
    if payload is None or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "未认证或 Token 已过期，请重新登录"},
        )
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "无效的 Token"},
        )
    result = await db.execute(select(User).where(User.id == UUID(user_id)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "用户不存在"},
        )
    return user


# ── Repository 依赖注入 ──────────────────────────────────────────────────────

_cache_repo: FallbackCacheRepository | None = None


def get_cache_repository() -> FallbackCacheRepository:
    """获取缓存仓库单例（优先 Redis，降级到内存）。"""
    global _cache_repo
    if _cache_repo is None:
        _cache_repo = FallbackCacheRepository()
    return _cache_repo


def get_test_cache_repository() -> InMemoryCacheRepository:
    """测试用：返回纯内存缓存。"""
    return InMemoryCacheRepository()


# ── 画布 Repository ──────────────────────────────────────────────────────────

def get_canvas_repository(db: AsyncSession = Depends(get_db)) -> CanvasRepository:
    """获取画布仓库实例。"""
    return SQLAlchemyCanvasRepository(db)


def get_chart_config_repository(
    db: AsyncSession = Depends(get_db),
) -> ChartConfigRepository:
    """获取图表配置仓库实例。"""
    return SQLAlchemyChartConfigRepository(db)


# ── 仪表板 Repository ────────────────────────────────────────────────────────

def get_dashboard_repository(
    db: AsyncSession = Depends(get_db),
) -> DashboardRepository:
    """获取仪表板仓库实例。"""
    return SQLAlchemyDashboardRepository(db)


def get_dashboard_chart_repository(
    db: AsyncSession = Depends(get_db),
) -> DashboardChartRepository:
    """获取仪表板图表仓库实例。"""
    return SQLAlchemyDashboardChartRepository(db)


# ── 报告 Repository ──────────────────────────────────────────────────────────

def get_report_repository(db: AsyncSession = Depends(get_db)) -> ReportRepository:
    """获取报告仓库实例。"""
    return SQLAlchemyReportRepository(db)


# ── 数据源 Repository ────────────────────────────────────────────────────────

def get_datasource_repository(
    db: AsyncSession = Depends(get_db),
) -> DataSourceRepository:
    """获取数据源仓库实例。"""
    return SQLAlchemyDataSourceRepository(db)


# ── 用户偏好 Repository ──────────────────────────────────────────────────────

def get_user_preference_repository(
    db: AsyncSession = Depends(get_db),
) -> UserPreferenceRepository:
    """获取用户偏好仓库实例。"""
    return SQLAlchemyUserPreferenceRepository(db)


# ── Service 依赖注入 ─────────────────────────────────────────────────────────

def get_canvas_service(
    canvas_repo: CanvasRepository = Depends(get_canvas_repository),
    chart_config_repo: ChartConfigRepository = Depends(get_chart_config_repository),
) -> CanvasService:
    """获取画布服务实例。"""
    return CanvasService(canvas_repo=canvas_repo, chart_config_repo=chart_config_repo)


def get_dashboard_service(
    dashboard_repo: DashboardRepository = Depends(get_dashboard_repository),
    dashboard_chart_repo: DashboardChartRepository = Depends(get_dashboard_chart_repository),
    db: AsyncSession = Depends(get_db),
) -> DashboardService:
    """获取仪表板服务实例。"""
    return DashboardService(
        dashboard_repo=dashboard_repo,
        dashboard_chart_repo=dashboard_chart_repo,
        db=db,
    )


def get_report_service(
    report_repo: ReportRepository = Depends(get_report_repository),
) -> ReportService:
    """获取报告服务实例。"""
    return ReportService(report_repo=report_repo)


def get_datasource_service(
    datasource_repo: DataSourceRepository = Depends(get_datasource_repository),
) -> DataSourceService:
    """获取数据源服务实例。"""
    return DataSourceService(datasource_repo=datasource_repo)


def get_user_preference_service(
    user_preference_repo: UserPreferenceRepository = Depends(
        get_user_preference_repository
    ),
) -> UserPreferenceService:
    """获取用户偏好服务实例。"""
    return UserPreferenceService(user_preference_repo=user_preference_repo)


# ── 新增：User / Auth / Notification / AI / DataQuality Repository ──────────


def get_user_repository(db: AsyncSession = Depends(get_db)) -> SQLAlchemyUserRepository:
    """获取用户仓库实例。"""
    return SQLAlchemyUserRepository(db)


def get_notification_repository(
    db: AsyncSession = Depends(get_db),
) -> SQLAlchemyNotificationRepository:
    """获取通知仓库实例。"""
    return SQLAlchemyNotificationRepository(db)


def get_ai_session_repository(
    db: AsyncSession = Depends(get_db),
) -> SQLAlchemyAISessionRepository:
    """获取 AI 会话仓库实例。"""
    return SQLAlchemyAISessionRepository(db)


def get_ai_message_repository(
    db: AsyncSession = Depends(get_db),
) -> SQLAlchemyAIMessageRepository:
    """获取 AI 消息仓库实例。"""
    return SQLAlchemyAIMessageRepository(db)


def get_datasource_schema_repository(
    db: AsyncSession = Depends(get_db),
) -> SQLAlchemyDataSourceSchemaRepository:
    """获取数据源 Schema（只读）仓库实例。"""
    return SQLAlchemyDataSourceSchemaRepository(db)


# ── Service 依赖注入（新增） ────────────────────────────────────────────────


def get_auth_service(
    user_repo: SQLAlchemyUserRepository = Depends(get_user_repository),
) -> AuthService:
    """获取认证服务实例。"""
    return AuthService(user_repo=user_repo)


def get_notification_service(
    notif_repo: SQLAlchemyNotificationRepository = Depends(get_notification_repository),
) -> NotificationService:
    """获取通知服务实例。"""
    return NotificationService(notif_repo=notif_repo)


def get_data_quality_service(
    schema_repo: SQLAlchemyDataSourceSchemaRepository = Depends(
        get_datasource_schema_repository
    ),
) -> DataQualityService:
    """获取数据质量检测服务实例。"""
    return DataQualityService(schema_repo=schema_repo)
