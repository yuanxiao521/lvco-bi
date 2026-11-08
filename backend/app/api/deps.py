from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token, token_version_of
from app.models.user import User, UserRole
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
    CacheRepository,
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


def assert_token_state(payload: dict, user: User) -> None:
    """校验"令牌"与"用户当前状态"是否还匹配（P0 止血）。

    两条规则：
    - 账号已停用（is_active=False）→ **403**。停用是比"令牌过期"更强的信号，
      不该被"请重新登录"掩盖（用户重新登录也没用，得找管理员）。
    - token_version 与 users.token_version 不一致 → **401**：登出/改密已把该账号
      所有已签发令牌作废，重新登录即可恢复。

    兼容性：令牌里没有 token_version claim（升级前签发的老 token）按 0 处理，
    与 DB 默认值 0 一致 → **存量用户不会被强制重新登录**。

    为什么抽成公开函数而不是内联在 get_current_user：本项目不止一条鉴权路径。
    `app/api/v1/notifications.py` 的 SSE 端点因为 EventSource 不能带自定义头，
    自行 decode 了 token（见该文件 :160-184）。它同样需要调用本函数，否则
    "登出/停用后旧 token 仍能订阅通知流"，止血会出现旁路。
    """
    if getattr(user, "is_active", True) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "账号已停用，请联系管理员"},
        )
    claim_version = token_version_of(payload)
    current_version = int(getattr(user, "token_version", 0) or 0)
    if claim_version != current_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "登录状态已失效，请重新登录"},
        )


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
    # P0 止血：仅"令牌有效 + 用户存在"不够——还要看账号是否被停用、令牌是否已被吊销。
    assert_token_state(payload, user)
    return user


def require_editor(current_user: User = Depends(get_current_user)) -> User:
    """写操作闸门：**viewer 只读**（P3-B）。

    背景：改之前 role 几乎是装饰品——它只在 permissions.py / trash.py 两处生效，
    viewer 依然能建数据源、跑 AI、发分享链接。这里把"能不能写"抽成一个依赖，
    挂到写端点（POST/PUT/PATCH/DELETE）。**刻意不挂 GET**：只读账号仍要能看
    数据源、画布、仪表盘、报表，否则"只读"会退化成"不可用"。

    判定：
      - admin / editor → 放行（返回 current_user，端点需要时可复用）
      - viewer         → 403 {"code": "FORBIDDEN", "message": "只读账号不能执行该操作"}
      - role 缺失/未知 → 放行（模型侧 role 是 NOT NULL + 枚举，正常不会出现；
        这里不 fail-close，避免历史脏数据把所有人都挡在门外）

    注意 role 可能是枚举也可能是字符串（测试/旧数据），统一取 .value 比较。
    """
    role = getattr(current_user, "role", None)
    role_value = getattr(role, "value", role)
    if role_value == UserRole.viewer.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "只读账号不能执行该操作"},
        )
    return current_user


# ── Repository 依赖注入 ──────────────────────────────────────────────────────

# 缓存单例已下沉到 app/core/cache.py：services 层（如 chat_task.py 执行内核）也要用它，
# 而 services → api 是反向依赖。这里 re-export 保持既有调用点零改动。
from app.core.cache import (  # noqa: E402
    get_cache_repository,
    get_test_cache_repository,
)


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
    cache_repo: CacheRepository = Depends(get_cache_repository),
) -> DashboardService:
    """获取仪表板服务实例。"""
    return DashboardService(
        dashboard_repo=dashboard_repo,
        dashboard_chart_repo=dashboard_chart_repo,
        db=db,
        cache_repo=cache_repo,
    )


def get_report_service(
    report_repo: ReportRepository = Depends(get_report_repository),
) -> ReportService:
    """获取报告服务实例。"""
    return ReportService(report_repo=report_repo)


def get_datasource_service(
    datasource_repo: DataSourceRepository = Depends(get_datasource_repository),
    cache_repo: CacheRepository = Depends(get_cache_repository),
) -> DataSourceService:
    """获取数据源服务实例（带缓存仓库，变更时自动清缓存）。"""
    return DataSourceService(datasource_repo=datasource_repo, cache_repo=cache_repo)


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
