"""数据源仓库实现

基于 SQLAlchemy 实现 DataSourceRepository 协议
"""
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.datasource import DataSource, DatasourceStatus, SourceType
from app.repositories.protocols import DataSourceRepository

logger = logging.getLogger(__name__)


class SQLAlchemyDataSourceRepository(DataSourceRepository):
    """基于 SQLAlchemy 的数据源仓库实现"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        user_id: UUID,
        name: str,
        source_type: str,
        file_path: str | None,
        connection_config: dict | None,
        status: str,
        size_bytes: int,
    ) -> DataSource:
        """创建数据源"""
        logger.debug(
            f"create: user_id={user_id}, name={name}, source_type={source_type}"
        )

        datasource = DataSource(
            user_id=user_id,
            name=name,
            source_type=SourceType(source_type),
            file_path=file_path,
            connection_config=connection_config,
            status=DatasourceStatus(status),
            size_bytes=size_bytes,
        )
        self.db.add(datasource)
        await self.db.flush()
        await self.db.refresh(datasource)

        logger.debug(f"create: created, id={datasource.id}")

        return datasource

    async def list_datasources(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        source_type: str | None,
        status: str | None,
        search: str | None,
    ) -> tuple[list[DataSource], int]:
        """查询数据源列表（分页）"""
        logger.debug(
            f"list_datasources: user_id={user_id}, page={page}, page_size={page_size}, "
            f"source_type={source_type}, status={status}, search={search}"
        )

        query = select(DataSource).where(DataSource.user_id == user_id)
        count_query = select(func.count()).select_from(DataSource).where(DataSource.user_id == user_id)

        if source_type is not None:
            st = SourceType(source_type)
            query = query.where(DataSource.source_type == st)
            count_query = count_query.where(DataSource.source_type == st)
        if status is not None:
            ds_status = DatasourceStatus(status)
            query = query.where(DataSource.status == ds_status)
            count_query = count_query.where(DataSource.status == ds_status)
        if search:
            query = query.where(DataSource.name.ilike(f"%{search}%"))
            count_query = count_query.where(DataSource.name.ilike(f"%{search}%"))

        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        query = query.order_by(DataSource.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        result = await self.db.execute(query)
        items = list(result.scalars().all())

        logger.debug(f"list_datasources: found {len(items)} datasources, total={total}")

        return items, total

    async def get_by_id(self, datasource_id: UUID, user_id: UUID) -> DataSource | None:
        """根据 ID 查询数据源"""
        logger.debug(f"get_by_id: datasource_id={datasource_id}, user_id={user_id}")

        result = await self.db.execute(
            select(DataSource).where(DataSource.id == datasource_id, DataSource.user_id == user_id)
        )
        datasource = result.scalar_one_or_none()

        if datasource:
            logger.debug(f"get_by_id: found, name={datasource.name}")
        else:
            logger.debug(f"get_by_id: not found")

        return datasource

    async def update(self, datasource: DataSource, **kwargs: Any) -> DataSource:
        """更新数据源"""
        logger.debug(f"update: id={datasource.id}, fields={list(kwargs.keys())}")

        for key, value in kwargs.items():
            if hasattr(datasource, key):
                setattr(datasource, key, value)

        await self.db.flush()
        await self.db.refresh(datasource)

        logger.debug(f"update: updated, id={datasource.id}")

        return datasource

    async def delete(self, datasource: DataSource) -> None:
        """物理删除数据源（真正从数据库移除）"""
        logger.debug(f"delete: id={datasource.id}")

        self.db.delete(datasource)
        await self.db.flush()
        await self.db.commit()

        logger.debug("delete: committed")
