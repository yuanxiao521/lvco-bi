"""Repository layer for data access abstraction.

集中管理所有 Repository 实现和 UnitOfWork，
遵循 Repository 模式：
- 只通过 SQLAlchemy 操作数据库
- 方法返回 ORM 对象（由 Service 层转换为 DTO）
- 只调用 flush()/refresh()，不调用 commit()
"""
from __future__ import annotations

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
from app.repositories.notification_repository import SQLAlchemyNotificationRepository
from app.repositories.report_repository import SQLAlchemyReportRepository
from app.repositories.unit_of_work import UnitOfWork
from app.repositories.user_preference_repository import (
    SQLAlchemyUserPreferenceRepository,
)
from app.repositories.user_repository import SQLAlchemyUserRepository

__all__ = [
    # ── Repository 实现 ─────────────────────────────────────────────
    "SQLAlchemyCanvasRepository",
    "SQLAlchemyChartConfigRepository",
    "SQLAlchemyDashboardChartRepository",
    "SQLAlchemyDashboardRepository",
    "SQLAlchemyDataSourceRepository",
    "SQLAlchemyDataSourceSchemaRepository",
    "SQLAlchemyNotificationRepository",
    "SQLAlchemyReportRepository",
    "SQLAlchemyUserPreferenceRepository",
    "SQLAlchemyUserRepository",
    "SQLAlchemyAISessionRepository",
    "SQLAlchemyAIMessageRepository",
    # ── Unit of Work ────────────────────────────────────────────────
    "UnitOfWork",
]
