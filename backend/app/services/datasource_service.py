import asyncio
import logging
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
from app.services.connection_guard import ConnectionValidationError
from app.services.datasource_connection import (
    DatasourceConnectionError,
    credentials_for,
    ensure_attached,
    needs_secret_upgrade,
    resolve_credentials,
    sanitize_error,
    seal_connection_config,
    table_ref_for,
)
from app.services.sensitive_columns import mark_fields, mask_rows, sensitive_names
from app.services.storage_service import storage
from app.utils.crypto import seal_secret


class DataSourceService:
    def __init__(
        self,
        datasource_repo: DataSourceRepository,
        cache_repo: "FallbackCacheRepository | None" = None,
    ) -> None:
        self.datasource_repo = datasource_repo
        self.db = datasource_repo.db
        self.cache_repo = cache_repo

    def _invalidate_query_cache(self, datasource_id) -> None:
        """数据源变更后清掉该数据源的所有查询缓存（按前缀）。"""
        if self.cache_repo is None:
            return
        try:
            n = self.cache_repo.delete_by_prefix(f"query:{datasource_id}:")
            if n > 0:
                _log = logging.getLogger("lvco.datasources")
                _log.info(
                    "query_cache_invalidated datasource_id=%s deleted=%s",
                    str(datasource_id), n,
                )
        except Exception as e:  # noqa: BLE001
            # 缓存清理失败不影响主链路
            logging.getLogger("lvco.datasources").warning(
                "query_cache_invalidate_failed datasource_id=%s error=%s",
                str(datasource_id), e,
            )

    async def _check_metric_bindings(
        self, user_id: UUID, datasource_id, schema_meta
    ) -> list[dict]:
        """数据源 schema 变更后校验绑定指标是否断链（重传导致字段失效）。"""
        try:
            from app.services.metric_service import check_metric_field_bindings

            return await check_metric_field_bindings(
                self.db, user_id, datasource_id, schema_meta
            )
        except Exception:  # noqa: BLE001
            return []

    def _attach_broken_metrics(self, ds: DataSource, broken: list[dict]) -> None:
        """把断链指标清单挂到 schema_meta.broken_metrics，前端可据此提示用户。"""
        if not broken:
            return
        meta = dict(ds.schema_meta or {})
        meta["broken_metrics"] = broken
        ds.schema_meta = meta

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
            def _import_in_window(conn=connector, fp=str(file_path), sch=schema_name):
                # 方案 B：DUCKDB_READ_ONLY 模式下导入必须经写窗口独占升级；默认读写模式透传
                with duckdb_client.write_window():
                    return conn.load_to_duckdb(duckdb_client, fp, sch)

            table_name = await asyncio.to_thread(_import_in_window)
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
            # 重传后 schema 可能变化：校验绑定指标是否断链并挂到 schema_meta
            broken = await self._check_metric_bindings(user_id, ds.id, ds.schema_meta)
            if broken:
                self._attach_broken_metrics(ds, broken)
                await self.db.flush()
        except Exception as e:
            ds = await self.datasource_repo.update(
                ds,
                status=DatasourceStatus.disconnected.value,
                schema_meta={"error": str(e)},
            )
            raise

        return ds

    @staticmethod
    def datasource_name_for_table(table_name: str) -> str:
        """表名 → 展示名（与前端历史命名保持一致，避免已有习惯被打断）。"""
        text = str(table_name or "").replace("_", " ").strip()
        return text[:200] if text else "未命名表"

    async def create_connection(
        self, user_id: UUID, body: dict
    ) -> tuple["DBConnection", list[DataSource]]:
        """创建"一个连接 + N 张表"（凭据只存一份）。

        改造前：前端选了 3 张表就调 3 次本接口，每张表各自存一份密码密文；
        现在：连接行承载凭据与 TLS 配置，datasource 只记 connection_id + table_name。

        Raises:
            ConnectionValidationError: 参数不合法 / 没选表（400）；
            CredentialKeyMissingError: 未配置 DB_ENCRYPTION_KEY（503，fail-closed）。
        """
        source_type_value = body.get("source_type")
        if isinstance(source_type_value, SourceType):
            source_type = source_type_value
        else:
            source_type = SourceType(source_type_value or "postgresql")

        table_names = body.get("table_names") or body.get("tableNames") or []
        if isinstance(table_names, str):
            table_names = [table_names]
        if not table_names:
            single = body.get("table_name") or body.get("tableName")
            table_names = [single] if single else []
        table_names = [str(t).strip() for t in table_names if str(t or "").strip()]
        if not table_names:
            raise ConnectionValidationError("请至少选择一张表")

        from app.services.connection_service import ConnectionService

        connection = await ConnectionService(self.db).create(
            user_id,
            name=body.get("name") or "",
            source_type=source_type,
            host=body.get("host"),
            port=body.get("port"),
            db_name=body.get("db_name"),
            username=body.get("username"),
            password=body.get("password"),
            ssl_mode=body.get("ssl_mode") or "prefer",
            ssl_ca_pem=body.get("ssl_ca_pem"),
        )

        datasources: list[DataSource] = []
        for table_name in table_names:
            # datasource 上只保留非敏感连接元数据（历史字段名沿用，兼容旧读取路径）
            connection_config = {
                "table_name": table_name,
                "db_name": connection.db_name,
                "host": connection.host,
                "port": connection.port,
                "username": connection.username,
            }
            ds = DataSource(
                user_id=user_id,
                name=body.get("name") and len(table_names) == 1
                    and body.get("name") or self.datasource_name_for_table(table_name),
                source_type=source_type,
                connection_id=connection.id,
                connection=connection,
                connection_config=connection_config,
                status=DatasourceStatus.disconnected,
            )
            self.db.add(ds)
            await self.db.flush()
            await self.db.refresh(ds)
            datasources.append(ds)
        return connection, datasources

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

    async def _upgrade_stored_secret(self, ds: DataSource, conn_info: dict) -> None:
        """历史明文 / 旧 CBC 密文 → v2 密文（幂等；失败只告警，不影响同步）。"""
        if not needs_secret_upgrade(ds.connection_config):
            return
        plaintext = conn_info.get("password") or ""
        if not plaintext:
            return
        try:
            sealed = seal_secret(plaintext)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("lvco.datasources").warning(
                "secret_upgrade_skipped datasource_id=%s error=%s", str(ds.id), e
            )
            return
        config = dict(ds.connection_config or {})
        config["password"] = sealed
        config["secret_format"] = "v2"
        ds.connection_config = config
        await self.db.flush()

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
                with duckdb_client.write_window():
                    table_name = connector.load_to_duckdb(duckdb_client, ds.file_path, schema_name)
                ds.row_count = connector.get_row_count(duckdb_client, schema_name, table_name)
                meta = connector.get_schema_meta(duckdb_client, schema_name, table_name)
                meta["fields"] = mark_fields(meta.get("fields"))
                ds.schema_meta = meta
                ds.status = DatasourceStatus.connected
            except Exception as e:
                ds.status = DatasourceStatus.disconnected
                # 不覆盖 schema_meta，保留已有的字段信息
        elif ds.source_type in (SourceType.postgresql, SourceType.mysql) and ds.connection_config:
            # 外部库：凭据解析 + DuckDB ATTACH 统一走 datasource_connection（PG/MySQL 同一段代码）
            connector = (
                postgres_connector if ds.source_type == SourceType.postgresql else mysql_connector
            )
            table_scope = "public schema" if ds.source_type == SourceType.postgresql else "database"
            conn_info: dict = {}
            try:
                conn_info = credentials_for(ds)
                # 历史明文/旧 CBC 密文顺手升级为 v2 密文（幂等，失败不影响同步）
                await self._upgrade_stored_secret(ds, conn_info)
                schema_name = duckdb_client.get_schema_name(
                    user_id, ds.id, ds.name, db_name=conn_info.get("db_name", "")
                )
                # 同步需全新连接：force 跳过缓存，DETACH 后重建外部连接并重新拉 schema
                ensure_attached(ds, schema_name, force=True)
                tables = connector.list_tables(duckdb_client, schema_name)
                target_table = conn_info.get("table_name", "")
                if target_table and target_table in tables:
                    table_name = target_table
                elif target_table:
                    ds.status = DatasourceStatus.disconnected
                    available = "、".join(tables[:10]) or "（无）"
                    ds.schema_meta = {
                        "error": f"表 {target_table} 不在该库的 {table_scope} 中。可用表：{available}"
                    }
                    ds.last_synced_at = datetime.now(timezone.utc)
                    await self.db.flush()
                    await self.db.refresh(ds)
                    return ds
                elif tables:
                    table_name = tables[0]
                else:
                    ds.status = DatasourceStatus.connected
                    ds.schema_meta = {"fields": [], "note": f"No tables found in {table_scope}"}
                    ds.last_synced_at = datetime.now(timezone.utc)
                    await self.db.flush()
                    await self.db.refresh(ds)
                    return ds

                meta = connector.get_schema_meta(
                    duckdb_client, schema_name, table_name
                )
                # 敏感列治理：按列名识别 → 打标记 + 丢弃样例值（见 sensitive_columns.py）
                meta["fields"] = mark_fields(meta.get("fields"))
                ds.schema_meta = meta
                ds.row_count = connector.get_row_count(
                    duckdb_client, schema_name, table_name
                )
                ds.size_bytes = max(ds.row_count * 200, 0)  # 粗略估算 ~200 bytes/row
                ds.status = DatasourceStatus.connected
                ds.schema_meta["table_name"] = table_name
                ds.schema_meta["available_tables"] = tables
                # 连接级状态由"这一轮有没有连上"决定（多表共享一条连接）
                if ds.connection is not None:
                    ds.connection.status = DatasourceStatus.connected.value
            except DatasourceConnectionError as e:
                ds.status = DatasourceStatus.disconnected
                ds.schema_meta = {"error": str(e)}
            except Exception as e:  # noqa: BLE001
                ds.status = DatasourceStatus.disconnected
                ds.schema_meta = {
                    "error": sanitize_error(str(e), secrets=[conn_info.get("password")])
                }
        else:
            ds.status = DatasourceStatus.disconnected

        ds.last_synced_at = datetime.now(timezone.utc)
        # 同步后 schema 可能变化：校验绑定指标是否断链并挂到 schema_meta
        broken = await self._check_metric_bindings(user_id, ds.id, ds.schema_meta)
        if broken:
            self._attach_broken_metrics(ds, broken)
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
        # schema 变化可能让字段映射失效，清掉该数据源查询缓存
        self._invalidate_query_cache(datasource_id)
        return ds

    async def delete(self, datasource_id: UUID, user_id: UUID) -> bool:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return False

        if ds.file_path and ds.source_type in (SourceType.csv, SourceType.excel):
            schema_name = duckdb_client.get_schema_name(user_id, ds.id, ds.name)
            try:
                with duckdb_client.write_window():
                    duckdb_client.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            except Exception:
                pass

        connection = ds.connection
        schema_name = None
        if ds.connection_id is not None:
            schema_name = duckdb_client.get_schema_name(
                user_id, ds.id, ds.name, db_name=(connection.db_name if connection else "")
            )
        await self.datasource_repo.delete(ds)
        # 数据源删除后清掉该数据源查询缓存
        self._invalidate_query_cache(datasource_id)
        # 连接下没有表了就回收连接（凭据不留孤儿副本）
        if connection is not None:
            from app.services.connection_service import ConnectionService

            try:
                await ConnectionService(self.db).delete_if_orphan(
                    connection, schema_name=schema_name
                )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("lvco.datasources").warning(
                    "connection_reclaim_failed connection_id=%s error=%s",
                    str(connection.id), exc,
                )
        return True

    async def preview(
        self, datasource_id: UUID, user_id: UUID, limit: int
    ) -> dict | None:
        ds = await self.get_by_id(datasource_id, user_id)
        if ds is None:
            return None

        if ds.source_type in (SourceType.postgresql, SourceType.mysql):
            return self._preview_external(ds, user_id, limit)
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
            meta = ds.schema_meta if isinstance(ds.schema_meta, dict) else {}
            masked = mask_rows(
                columns, [list(row) for row in rows], sensitive_names(meta.get("fields"))
            )
            return {
                "datasource_id": str(ds.id),
                "columns": columns,
                "rows": masked,
                "total_rows": ds.row_count,
                "preview_rows": len(rows),
            }
        except Exception:
            return {"datasource_id": str(ds.id), "columns": [], "rows": [], "total_rows": 0, "preview_rows": 0}

    def _preview_external(self, ds: DataSource, user_id: UUID, limit: int) -> dict:
        """外部库（PG/MySQL）预览：ATTACH 后直接 SELECT LIMIT n。

        改造前这里对数据库型数据源直接返回空数组 —— 用户连上库却"看不到任何数据"，
        是最典型的"能连不能用"体验缺口。
        """
        empty = {"datasource_id": str(ds.id), "columns": [], "rows": [], "total_rows": 0, "preview_rows": 0}
        try:
            conn_info = credentials_for(ds)
            schema_name = duckdb_client.get_schema_name(
                user_id, ds.id, ds.name, db_name=conn_info.get("db_name", "")
            )
            ensure_attached(ds, schema_name)
            meta = ds.schema_meta if isinstance(ds.schema_meta, dict) else {}
            table_name = meta.get("table_name") or ""
            if not table_name:
                return empty
            ref = table_ref_for(
                schema_name, ds.source_type, table_name, database=conn_info.get("db_name", "")
            )
            fields = [f.get("name") for f in (meta.get("fields") or []) if isinstance(f, dict) and f.get("name")]
            rows = duckdb_client.fetchall(f"SELECT * FROM {ref} LIMIT {int(limit)}")
            columns = fields or [f"c{i}" for i in range(len(rows[0]) if rows else 0)]
            # 敏感列在预览里直接打码：预览是最容易被截图/复制的界面
            masked = mask_rows(
                columns, [list(row) for row in rows], sensitive_names(meta.get("fields"))
            )
            return {
                "datasource_id": str(ds.id),
                "columns": columns,
                "rows": masked,
                "total_rows": ds.row_count,
                "preview_rows": len(rows),
            }
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("lvco.datasources").warning(
                "preview_external_failed datasource_id=%s error=%s",
                str(ds.id),
                sanitize_error(str(exc), limit=160),
            )
            return empty
