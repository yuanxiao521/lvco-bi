"""数据库连接服务：一份凭据 → 多张表。

职责边界：
- 凭据的落库形态（AES-256-GCM 密文）与生命周期（轮换）在这里；
- 与 DuckDB 的交互（SECRET + ATTACH）仍走 datasource_connection；
- 数据源（表）级的同步/字段语义留在 datasource_service。

为什么值得单独一层：
1. 轮换密码要"一次改、全体生效"，必须有一个连接级对象承载凭据；
2. 健康检查是连接级操作（同一个 host 不需要按表探测 N 次）；
3. TLS 配置（模式 + CA）属于连接，不属于表。
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.connectors.mysql_connector import mysql_connector
from app.connectors.postgres_connector import postgres_connector
from app.core.duckdb_client import duckdb_client
from app.models.datasource import DataSource, DatasourceStatus, SourceType
from app.models.db_connection import ConnectionHealth, DBConnection
from app.services.connection_guard import ConnectionValidationError, validate_ssl
from app.services.datasource_connection import (
    DatasourceConnectionError,
    attach_connection,
    build_connection_columns,
    connection_credentials,
    connection_summary,
    detach,
    ensure_attached,
    materialize_ca,
    sanitize_error,
)
from app.utils.crypto import seal_secret

logger = structlog.get_logger("connection_service")


class ConnectionServiceError(RuntimeError):
    """连接级操作失败（message 可直接展示）。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _schema_name_for(connection: DBConnection, datasource: DataSource) -> str:
    """连接下某张表在 DuckDB 里的 catalog 名（与 sync 时保持一致）。"""
    return duckdb_client.get_schema_name(
        connection.user_id, datasource.id, datasource.name, db_name=connection.db_name or ""
    )


class ConnectionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── 查询 ───────────────────────────────────────────────────────

    async def get(self, connection_id: UUID, user_id: UUID) -> DBConnection | None:
        result = await self.db.execute(
            select(DBConnection).where(
                DBConnection.id == connection_id, DBConnection.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def list(
        self, user_id: UUID, page: int = 1, page_size: int = 20
    ) -> tuple[list[DBConnection], int]:
        total = (
            await self.db.execute(
                select(func.count())
                .select_from(DBConnection)
                .where(DBConnection.user_id == user_id)
            )
        ).scalar() or 0
        result = await self.db.execute(
            select(DBConnection)
            .where(DBConnection.user_id == user_id)
            .order_by(DBConnection.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(result.scalars().all()), int(total)

    @staticmethod
    def datasource_count(connection: DBConnection) -> int:
        return len(getattr(connection, "datasources", []) or [])

    # ── 创建 / 更新 ────────────────────────────────────────────────

    async def create(
        self,
        user_id: UUID,
        *,
        name: str,
        source_type,
        host: str,
        port,
        db_name: str,
        username: str,
        password: str | None,
        ssl_mode: str = "prefer",
        ssl_ca_pem: str | None = None,
    ) -> DBConnection:
        """创建连接行（校验 + 加密）。同一用户下相同 host/port/db/user 复用已有连接。"""
        columns = build_connection_columns(
            host=host, port=port, db_name=db_name, username=username,
            password=password, ssl_mode=ssl_mode, ssl_ca_pem=ssl_ca_pem,
            source_type=source_type,
        )
        existing = (
            await self.db.execute(
                select(DBConnection).where(
                    DBConnection.user_id == user_id,
                    DBConnection.source_type == str(getattr(source_type, "value", source_type)),
                    DBConnection.host == columns["host"],
                    DBConnection.port == columns["port"],
                    DBConnection.db_name == columns["db_name"],
                    DBConnection.username == columns["username"],
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # 同一个物理连接：复用凭据（密码以本次提交为准），避免多份副本
            existing.password_enc = columns["password_enc"] or existing.password_enc
            existing.ssl_mode = columns["ssl_mode"]
            existing.ssl_ca_enc = columns["ssl_ca_enc"] or existing.ssl_ca_enc
            if name:
                existing.name = name
            await self.db.flush()
            await self.db.refresh(existing)
            return existing

        connection = DBConnection(
            user_id=user_id,
            name=name or columns["db_name"] or "database",
            source_type=str(getattr(source_type, "value", source_type)),
            host=columns["host"],
            port=columns["port"],
            db_name=columns["db_name"],
            username=columns["username"],
            password_enc=columns["password_enc"],
            ssl_mode=columns["ssl_mode"],
            ssl_ca_enc=columns["ssl_ca_enc"],
            status=DatasourceStatus.disconnected.value,
        )
        self.db.add(connection)
        await self.db.flush()
        await self.db.refresh(connection)
        logger.info(
            "connection_created connection_id=%s dsn=%s tls=%s",
            connection.id, connection_summary(connection), connection.ssl_mode,
        )
        return connection

    async def update(
        self,
        connection_id: UUID,
        user_id: UUID,
        *,
        name: str | None = None,
        ssl_mode: str | None = None,
        ssl_ca_pem: str | None = None,
    ) -> DBConnection | None:
        connection = await self.get(connection_id, user_id)
        if connection is None:
            return None
        if name is not None and str(name).strip():
            connection.name = str(name).strip()[:200]

        ssl_changed = False
        if ssl_mode is not None or ssl_ca_pem is not None:
            mode, ca = validate_ssl(
                ssl_mode if ssl_mode is not None else connection.ssl_mode,
                ssl_ca_pem if ssl_ca_pem is not None else None,
            )
            if mode != connection.ssl_mode:
                ssl_changed = True
            connection.ssl_mode = mode
            if ssl_ca_pem is not None:
                connection.ssl_ca_enc = seal_secret(ca) if ca else None
                ssl_changed = True
        await self.db.flush()

        if ssl_changed:
            # TLS 变更必须重建 ATTACH，否则旧连接还在用老参数
            await self.reattach_children(connection, force=True)
            connection.health_status = None
            connection.health_error = None
            await self.db.flush()
        await self.db.refresh(connection)
        return connection

    # ── 轮换 ───────────────────────────────────────────────────────

    async def rotate_secret(
        self, connection_id: UUID, user_id: UUID, new_password: str
    ) -> dict:
        """轮换密码：先验证新密码能连上，再加密落库并重建所有表连接。"""
        connection = await self.get(connection_id, user_id)
        if connection is None:
            raise ConnectionServiceError("连接不存在")
        password = "" if new_password is None else str(new_password)
        if not password:
            raise ConnectionValidationError("新密码不能为空")

        # 1) 用新密码探测（不写库），避免把不可用的凭据覆盖进去
        creds = connection_credentials(connection)
        creds["password"] = password
        ok, error = await self._probe_creds(connection, creds)
        if not ok:
            raise ConnectionValidationError(f"新密码验证失败：{error or '无法连接'}")

        # 2) 加密落库
        connection.password_enc = seal_secret(password)
        connection.health_status = ConnectionHealth.ok.value
        connection.health_error = None
        connection.last_health_at = _utcnow()
        connection.status = DatasourceStatus.connected.value
        await self.db.flush()

        # 3) 重建该连接下所有表的 ATTACH（只有真的连上的才标 connected）
        reattach = await self.reattach_children(connection, force=True)
        rotated = reattach["ok"]
        for ds in rotated:
            ds.last_synced_at = ds.last_synced_at or _utcnow()
        await self.db.flush()
        for ds in rotated + reattach["failed"]:
            await self.db.refresh(ds)
        logger.info(
            "connection_secret_rotated connection_id=%s ok=%s failed=%s",
            connection.id, len(rotated), len(reattach["failed"]),
        )
        return {"verified": True, "datasources": rotated, "failed": reattach["failed"]}

    async def reattach_children(self, connection: DBConnection, *, force: bool = True) -> dict:
        """重建连接下所有表的 DuckDB ATTACH（TLS/密码变更后调用）。

        Returns:
            {"ok": [DataSource], "failed": [DataSource]}——交给调用方决定状态，
            避免"重建失败却把表标成 connected"的假绿。
        """
        ok, failed = [], []
        for ds in list(connection.datasources or []):
            schema_name = _schema_name_for(connection, ds)
            try:
                # 统一入口：ensure_attached 会自动读到 datasource.connection 的凭据/TLS
                ensure_attached(ds, schema_name, force=force)
            except DatasourceConnectionError as exc:
                ds.status = DatasourceStatus.disconnected
                meta = dict(ds.schema_meta or {})
                meta["error"] = sanitize_error(str(exc), limit=200)
                ds.schema_meta = meta
                failed.append(ds)
                logger.warning(
                    "connection_reattach_failed connection_id=%s datasource_id=%s error=%s",
                    connection.id, ds.id, sanitize_error(str(exc), limit=120),
                )
            else:
                # 连上了：清掉上一轮留下的连接错误并恢复状态
                meta = dict(ds.schema_meta or {})
                if meta.pop("error", None) is not None:
                    ds.schema_meta = meta
                if ds.status != DatasourceStatus.connected:
                    ds.status = DatasourceStatus.connected
                ok.append(ds)
        await self.db.flush()
        return {"ok": ok, "failed": failed}

    # ── 健康检查 ───────────────────────────────────────────────────

    async def _probe_creds(self, connection: DBConnection, creds: dict) -> tuple[bool, str | None]:
        ca_path = materialize_ca(connection.id, creds.get("ssl_ca_pem"))
        if ca_path:
            creds = {**creds, "ssl_ca_path": ca_path}
        connector = (
            mysql_connector
            if str(connection.source_type) == SourceType.mysql.value
            else postgres_connector
        )
        try:
            result = await connector.test_connection(creds)
        except Exception as exc:  # noqa: BLE001
            return False, sanitize_error(str(exc), secrets=[creds.get("password")])
        if result.get("success"):
            return True, None
        return False, sanitize_error(
            str(result.get("error") or "连接失败"), secrets=[creds.get("password")]
        )

    async def probe(self, connection: DBConnection) -> tuple[bool, str | None]:
        try:
            creds = connection_credentials(connection)
        except DatasourceConnectionError as exc:
            return False, str(exc)
        return await self._probe_creds(connection, creds)

    async def check_health(
        self, connection_id: UUID, user_id: UUID, *, force: bool = False
    ) -> dict | None:
        connection = await self.get(connection_id, user_id)
        if connection is None:
            return None

        min_age = int(settings.CONNECTION_HEALTH_MIN_AGE_MINUTES)
        if not force and connection.last_health_at is not None:
            age = _utcnow() - connection.last_health_at
            if age < timedelta(minutes=min_age):
                return self._health_payload(connection)

        ok, error = await self.probe(connection)
        connection.health_status = (
            ConnectionHealth.ok.value if ok else ConnectionHealth.failed.value
        )
        connection.health_error = None if ok else sanitize_error(error or "", limit=280)
        connection.last_health_at = _utcnow()
        connection.status = (
            DatasourceStatus.connected.value if ok else DatasourceStatus.disconnected.value
        )
        await self.db.flush()
        await self.db.refresh(connection)
        logger.info(
            "connection_health_checked connection_id=%s status=%s error=%s",
            connection.id, connection.health_status, connection.health_error,
        )
        return self._health_payload(connection)

    @staticmethod
    def _health_payload(connection: DBConnection) -> dict:
        return {
            "status": connection.health_status,
            "error": connection.health_error,
            "checkedAt": connection.last_health_at,
        }

    # ── 删除 ───────────────────────────────────────────────────────

    async def delete_if_orphan(
        self, connection: DBConnection, *, schema_name: str | None = None
    ) -> bool:
        """连接下已经没有表了就回收（并摘掉对应 DuckDB catalog）。"""
        remaining = (
            await self.db.execute(
                select(func.count())
                .select_from(DataSource)
                .where(DataSource.connection_id == connection.id)
            )
        ).scalar() or 0
        if remaining:
            return False
        if schema_name:
            detach(schema_name)
        await self.db.delete(connection)
        await self.db.flush()
        logger.info("connection_reclaimed connection_id=%s", connection.id)
        return True
