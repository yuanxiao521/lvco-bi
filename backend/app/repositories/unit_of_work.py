"""UnitOfWork（工作单元）模式。

设计目的：
- 将事务控制集中到一个语义单元
- 取代"Service 直接调用 db.commit()"的反模式
- 同时托管所有 Repository 实例，保证它们共享同一个事务

使用方式：

```python
# FastAPI 依赖注入
async def get_uow(db: AsyncSession = Depends(get_db)) -> AsyncIterator[UnitOfWork]:
    async with UnitOfWork(db) as uow:
        yield uow

# 路由层
@router.post("/...")
async def handler(uow: UnitOfWork = Depends(get_uow)):
    dashboard = await uow.dashboard_repo.create(...)
    await uow.dashboard_chart_repo.add_chart(...)
    await uow.commit()  # 唯一提交点
    # 失败时自动 rollback
```

业务约束：
- Repository 通过 `await uow.commit()` 提交，永不自己 commit
- 异常自动 rollback
- Repository 仍只调用 flush()/refresh()，不调用 commit()
"""
from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
from app.repositories.protocols import (
    DashboardChartRepository,
    DashboardRepository,
)
from app.repositories.report_repository import SQLAlchemyReportRepository
from app.repositories.user_preference_repository import (
    SQLAlchemyUserPreferenceRepository,
)
from app.repositories.user_repository import SQLAlchemyUserRepository

logger = logging.getLogger(__name__)


class UnitOfWork:
    """工作单元（Unit of Work）。

    封装：
    - 所有 Repository 实例（共享同一 AsyncSession）
    - 事务控制（commit / rollback）
    - 上下文管理（async with）

    所有 Repository 都可以通过属性访问：

        uow.dashboard_repo
        uow.dashboard_chart_repo
        uow.canvas_repo
        uow.chart_config_repo
        uow.report_repo
        uow.datasource_repo
        uow.datasource_schema_repo
        uow.user_repo
        uow.user_preference_repo
        uow.notification_repo
        uow.ai_session_repo
        uow.ai_message_repo
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        # ── 所有 Repository 共享同一个 db ─────────────────────────────
        self.dashboard_repo: DashboardRepository = SQLAlchemyDashboardRepository(db)
        self.dashboard_chart_repo: DashboardChartRepository = (
            SQLAlchemyDashboardChartRepository(db)
        )
        self.canvas_repo = SQLAlchemyCanvasRepository(db)
        self.chart_config_repo = SQLAlchemyChartConfigRepository(db)
        self.report_repo = SQLAlchemyReportRepository(db)
        self.datasource_repo = SQLAlchemyDataSourceRepository(db)
        self.datasource_schema_repo = SQLAlchemyDataSourceSchemaRepository(db)
        self.user_repo = SQLAlchemyUserRepository(db)
        self.user_preference_repo = SQLAlchemyUserPreferenceRepository(db)
        self.notification_repo = SQLAlchemyNotificationRepository(db)
        self.ai_session_repo = SQLAlchemyAISessionRepository(db)
        self.ai_message_repo = SQLAlchemyAIMessageRepository(db)

    async def __aenter__(self) -> "UnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """退出 with 块时，如果发生异常自动 rollback。"""
        if exc_type is not None:
            await self.rollback()

    async def commit(self) -> None:
        """提交事务。"""
        try:
            await self.db.commit()
            logger.debug("uow_committed")
        except Exception as e:
            logger.error(f"uow_commit_failed: {e}")
            await self.db.rollback()
            raise

    async def rollback(self) -> None:
        """回滚事务。"""
        try:
            await self.db.rollback()
            logger.debug("uow_rolled_back")
        except Exception as e:
            logger.error(f"uow_rollback_failed: {e}")
            raise

    async def flush(self) -> None:
        """强制 flush（不 commit）。"""
        await self.db.flush()
