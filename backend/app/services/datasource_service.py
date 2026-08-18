import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile

from app.config import settings
from app.connectors.csv_connector import CSVConnector
from app.connectors.excel_connector import ExcelConnector
from app.connectors.mysql_connector import mysql_connector
from app.connectors.postgres_connector import postgres_connector
from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource, DatasourceStatus, SourceType
from app.repositories.protocols import DataSourceRepository
from app.schemas import DataSourceUpdate
from app.services.storage_service import storage
from app.utils.crypto import decrypt_value, get_encryption_key


class DataSourceService:
    def __init__(self, datasource_repo: DataSourceRepository) -> None:
        self.datasource_repo = datasource_repo
        self.db = datasource_repo.db

    async def list_datasources(
        self,
        user_id: UUID,
        page: int = 1,
        page_size: int = 20,
        source_type: SourceType | None = None,
        status: DatasourceStatus | None = None,
        search: str | None = None,
    ) -> tuple[list[DataSource], int]:
        return await self.datasource_repo.list_datasources(
            user_id=user_id,
            page=page,
            page_size=page_size,
            source_type=source_type.value if source_type else None,
            status=status.value if status else None,
            search=search,
        )

    async def get_by_id(self, datasource_id: UUID, user_id: UUID) -> DataSource | None:
        return await self.datasource_repo.get_by_id(datasource_id, user_id)

    async def upload_file(self, user_id: UUID, name: str, file: UploadFile) -> DataSource:
        if file.size and file.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            raise ValueError(f"文件大小超过限制（最大 {settings.MAX_UPLOAD_SIZE_MB}MB）")

        upload_dir = Path(settings.UPLOAD_DIR) / f"user_{user_id}"
        upload_dir.mkdir(parents=True, exist_ok=True)

        ext = Path(file.filename or "").suffix.lower()
        if ext not in (".csv", ".xlsx", ".xls"):
            raise ValueError(f"不支持的文件格式: {ext}，仅支持 .csv / .xlsx / .xls")

        file_id = str(uuid.uuid4())
        saved_filename = f"{file_id}{ext}"
        file_path = upload_dir / saved_filename

        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)

        file_size = os.path.getsize(file_path)

        if ext == ".csv":
            source_type = SourceType.csv
            connector = CSVConnector()
        else:
            source_type = SourceType.excel
            connector = ExcelConnector()

        ds = await self.datasource_repo.create(
            user_id=user_id,
            name=name,
            source_type=source_type.value,
            file_path=str(file_path),
            connection_config=None,
            status=DatasourceStatus.syncing.value,
            size_bytes=file_size,
        )

        # Try MinIO upload (falls back to local file if unavailable)
        minio_key = f"datasources/{ds.id}/{file.filename}"
        storage.put_object(
            settings.minio_bucket,
            minio_key,
            content,
            file.content_type or "text/csv",
        )

        schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name)
        try:
            table_name = await asyncio.to_thread(
                connector.load_to_duckdb, duckdb_client, str(file_path), schema_name
            )
            schema_meta = await asyncio.to_thread(
                connector.get_schema_meta, duckdb_client, schema_name, table_name
            )
            row_count = await asyncio.to_thread(
                connector.get_row_count, duckdb_client, schema_name, table_name
            )

            ds = await self.datasource_repo.update(
                ds,
                schema_meta=schema_meta,
                row_count=row_count,
                status=DatasourceStatus.connected.value,
                last_synced_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            ds = await self.datasource_repo.update(
                ds,
                status=DatasourceStatus.disconnected.value,
                schema_meta={"error": str(e)},
            )
            raise

        return ds

    async def create_connection(self, user_id: UUID, body: dict) -> DataSource:
        source_type_value = body.get("source_type")
        if isinstance(source_type_value, SourceType):
            source_type = source_type_value
        else:
            source_type = SourceType(source_type_value or "postgresql")
        ds = DataSource(
            user_id=user_id,
            name=body.get("name", ""),
            source_type=source_type,
            connection_config={
                "host": body.get("host"),
                "port": body.get("port"),
                "db_name": body.get("db_name"),
                "username": body.get("username"),
                "password": body.get("password"),
                "table_name": body.get("table_name", body.get("tableName", "")),
            },
            status=DatasourceStatus.disconnected,
        )
        self.db.add(ds)
        await self.db.flush()
        await self.db.refresh(ds)
        return ds

    async def update_status(
        self, datasource_id: UUID, user_id: UUID, new_status: DatasourceStatus
    ) -> DataSource | None:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return None
        ds.status = new_status
        await self.db.flush()
        await self.db.refresh(ds)
        return ds

    async def sync_datasource(self, datasource_id: UUID, user_id: UUID) -> DataSource | None:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return None
        ds.status = DatasourceStatus.syncing
        await self.db.flush()

        if ds.file_path and ds.source_type in (SourceType.csv, SourceType.excel):
            schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name)
            if ds.source_type == SourceType.csv:
                connector = CSVConnector()
            else:
                connector = ExcelConnector()
            try:
                table_name = connector.load_to_duckdb(duckdb_client, ds.file_path, schema_name)
                ds.row_count = connector.get_row_count(duckdb_client, schema_name, table_name)
                ds.schema_meta = connector.get_schema_meta(duckdb_client, schema_name, table_name)
                ds.status = DatasourceStatus.connected
            except Exception as e:
                ds.status = DatasourceStatus.disconnected
                # 不覆盖 schema_meta，保留已有的字段信息
        elif ds.source_type == SourceType.postgresql and ds.connection_config:
            # Use DuckDB's PostgreSQL ATTACH to sync schema info
            conn_info = dict(ds.connection_config)
            schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name, db_name=conn_info.get("db_name", ""))
            # 解密密码
            key = get_encryption_key()
            password = conn_info.get("password", "")
            if key and password:
                conn_info["password"] = decrypt_value(password, key)
            # PostgresConnector stores: host, port, db_name, username, password
            conn_info["host"] = conn_info.get("host", "localhost")
            conn_info["port"] = conn_info.get("port", 5432)
            conn_info["user"] = conn_info.get("username", "postgres")
            conn_info["database"] = conn_info.get("db_name", "")
            try:
                # 先 DETACH 再 ATTACH，保证同步时是全新连接（持久化文件中旧连接已失效）
                try:
                    duckdb_client.execute(f'DETACH "{schema_name}"')
                except Exception:
                    pass  # schema 不存在，忽略
                attach_sql = postgres_connector.get_attach_sql(conn_info, schema_name)
                duckdb_client.execute(attach_sql)
                tables = postgres_connector.list_tables(duckdb_client, schema_name)
                # Use specified table_name from connection_config, or fall back to first table
                target_table = conn_info.get("table_name", "")
                if target_table and target_table in tables:
                    table_name = target_table
                elif target_table:
                    # Table specified but not found in public schema
                    ds.status = DatasourceStatus.disconnected
                    ds.schema_meta = {"error": f"Table '{target_table}' not found in public schema"}
                    ds.last_synced_at = datetime.now(timezone.utc)
                    await self.db.flush()
                    await self.db.refresh(ds)
                    return ds
                elif tables:
                    table_name = tables[0]
                else:
                    ds.status = DatasourceStatus.connected
                    ds.schema_meta = {"fields": [], "note": "No public tables found"}
                    ds.last_synced_at = datetime.now(timezone.utc)
                    await self.db.flush()
                    await self.db.refresh(ds)
                    return ds

                ds.schema_meta = postgres_connector.get_schema_meta(
                    duckdb_client, schema_name, table_name
                )
                ds.row_count = postgres_connector.get_row_count(
                    duckdb_client, schema_name, table_name
                )
                ds.size_bytes = max(ds.row_count * 200, 0)  # 粗略估算 ~200 bytes/row
                ds.status = DatasourceStatus.connected
                # Store table name for later queries
                ds.schema_meta["table_name"] = table_name
                ds.schema_meta["available_tables"] = tables
            except Exception as e:
                ds.status = DatasourceStatus.disconnected
                ds.schema_meta = {"error": str(e)}
        elif ds.source_type == SourceType.mysql and ds.connection_config:
            # Use DuckDB's MySQL ATTACH to sync schema info
            conn_info = dict(ds.connection_config)
            schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name, db_name=conn_info.get("db_name", ""))
            # 解密密码
            key = get_encryption_key()
            password = conn_info.get("password", "")
            if key and password:
                conn_info["password"] = decrypt_value(password, key)
            conn_info["host"] = conn_info.get("host", "localhost")
            conn_info["port"] = conn_info.get("port", 3306)
            conn_info["user"] = conn_info.get("username", "root")
            conn_info["database"] = conn_info.get("db_name", "")
            try:
                # 先 DETACH 再 ATTACH
                try:
                    duckdb_client.execute(f'DETACH "{schema_name}"')
                except Exception:
                    pass
                attach_sql = mysql_connector.get_attach_sql(conn_info, schema_name)
                duckdb_client.execute(attach_sql)
                tables = mysql_connector.list_tables(duckdb_client, schema_name)
                target_table = conn_info.get("table_name", "")
                if target_table and target_table in tables:
                    table_name = target_table
                elif target_table:
                    ds.status = DatasourceStatus.disconnected
                    ds.schema_meta = {"error": f"Table '{target_table}' not found in database"}
                    ds.last_synced_at = datetime.now(timezone.utc)
                    await self.db.flush()
                    await self.db.refresh(ds)
                    return ds
                elif tables:
                    table_name = tables[0]
                else:
                    ds.status = DatasourceStatus.connected
                    ds.schema_meta = {"fields": [], "note": "No tables found"}
                    ds.last_synced_at = datetime.now(timezone.utc)
                    await self.db.flush()
                    await self.db.refresh(ds)
                    return ds

                ds.schema_meta = mysql_connector.get_schema_meta(
                    duckdb_client, schema_name, table_name
                )
                ds.row_count = mysql_connector.get_row_count(
                    duckdb_client, schema_name, table_name
                )
                ds.size_bytes = max(ds.row_count * 200, 0)  # 粗略估算 ~200 bytes/row
                ds.status = DatasourceStatus.connected
                ds.schema_meta["table_name"] = table_name
                ds.schema_meta["available_tables"] = tables
            except Exception as e:
                ds.status = DatasourceStatus.disconnected
                ds.schema_meta = {"error": str(e)}
        else:
            ds.status = DatasourceStatus.disconnected

        ds.last_synced_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(ds)
        return ds

    async def update_schema(
        self, datasource_id: UUID, user_id: UUID, body: DataSourceUpdate
    ) -> DataSource | None:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return None
        if body.schema_meta is not None:
            ds.schema_meta = body.schema_meta
        await self.db.flush()
        await self.db.refresh(ds)
        return ds

    async def delete(self, datasource_id: UUID, user_id: UUID) -> bool:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return False

        if ds.file_path and ds.source_type in (SourceType.csv, SourceType.excel):
            schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name)
            try:
                duckdb_client.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            except Exception:
                pass

        await self.datasource_repo.delete(ds)
        return True

    async def preview(
        self, datasource_id: UUID, user_id: UUID, limit: int
    ) -> dict | None:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return None

        if ds.source_type not in (SourceType.csv, SourceType.excel):
            return {"datasource_id": str(ds.id), "columns": [], "rows": [], "total_rows": 0, "preview_rows": 0}

        schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name)
        try:
            columns_result = duckdb_client.fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = 'data'",
                [schema_name],
            )
            if not columns_result:
                return {"datasource_id": str(ds.id), "columns": [], "rows": [], "total_rows": 0, "preview_rows": 0}

            columns = [row[0] for row in columns_result]
            rows = duckdb_client.fetchall(f'SELECT * FROM "{schema_name}"."data" LIMIT {limit}')
            return {
                "datasource_id": str(ds.id),
                "columns": columns,
                "rows": [list(row) for row in rows],
                "total_rows": ds.row_count,
                "preview_rows": len(rows),
            }
        except Exception:
            return {"datasource_id": str(ds.id), "columns": [], "rows": [], "total_rows": 0, "preview_rows": 0}
