"""DataSource Schema Repository（轻量级补充）。

DataQualityService 只需要从 `db` 读取一个 DataSource 记录，不需要
CRUD，所以单独抽出一个只读 Repository。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.datasource import DataSource


class SQLAlchemyDataSourceSchemaRepository:
    """DataSource Schema 只读 Repository。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, datasource_id: uuid.UUID | str) -> DataSource | None:
        """根据 ID 查询数据源（用于解析 schema 名）。"""
        if isinstance(datasource_id, str):
            datasource_id = uuid.UUID(datasource_id)
        result = await self.db.execute(
            select(DataSource).where(DataSource.id == datasource_id)
        )
        return result.scalar_one_or_none()

    async def get_schema_info(
        self, datasource_id: uuid.UUID | str
    ) -> tuple[str, str, dict[str, Any]] | None:
        """解析数据源的 DuckDB schema 名 + 表名 + 元数据。

        Returns:
            (schema_name, table_name, schema_meta) 元组，找不到则返回 None。
        """
        ds = await self.get_by_id(datasource_id)
        if ds is None:
            return None
        from app.core.duckdb_client import duckdb_client
        schema_name = duckdb_client.get_schema_name(ds.user_id, ds.id, ds.name)
        # 默认表名 "data"
        table_name = "data"
        schema_meta = ds.schema_meta or {}
        if ds.source_type and ds.source_type.value == "postgresql" and isinstance(schema_meta, dict):
            table_name = schema_meta.get("table_name", table_name)
        return schema_name, table_name, schema_meta
