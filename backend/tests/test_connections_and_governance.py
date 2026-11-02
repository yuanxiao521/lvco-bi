# -*- coding: utf-8 -*-
"""F1–F4 回归：连接模型（一份凭据多张表）/ TLS / 敏感列 / 轮换与健康。

对应能力：
- F1 连接：db_connections 承载凭据，datasource 只记 connection_id（凭据一份、表多份）
- F2 TLS：sslMode/CA 贯通驱动与 DuckDB SECRET（SSLMODE / SSL_MODE / SSLROOTCERT）
- F3 敏感列：识别、样例剔除、预览打码、SQL/图表配置拦截、工具层提示
- F4 轮换与健康：先验证再落库、连接级探测、失败不假绿
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.connectors.attach_sql import build_attach_statements
from app.services import connection_guard, sensitive_columns
from app.services.connection_service import ConnectionService, ConnectionServiceError
from app.services.datasource_connection import (
    build_connection_columns,
    connection_credentials,
    credentials_for,
    materialize_ca,
)
from app.services.datasource_service import DataSourceService
from app.schemas import ConnectionResponse
from app.utils.crypto import encrypt_value, is_encrypted

KEY_HEX = "33" * 32


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "db_encryption_key", KEY_HEX, raising=False)
    return bytes.fromhex(KEY_HEX)


def _now():
    return datetime.now(timezone.utc)


def _connection(**over):
    base = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), name="sales", source_type="postgresql",
        host="10.0.0.5", port=5432, db_name="sales", username="ro",
        password_enc=None, ssl_mode="prefer", ssl_ca_enc=None,
        status="disconnected", health_status=None, health_error=None,
        last_health_at=None, datasources=[], created_at=_now(), updated_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── F1：一份凭据 / 多张表 ────────────────────────────────────────────

def test_build_connection_columns_encrypts(with_key):
    cols = build_connection_columns(
        host="10.0.0.5", port=5432, db_name="sales", username="ro", password="pw",
    )
    assert is_encrypted(cols["password_enc"])
    assert cols["ssl_mode"] == "prefer"


def test_credentials_for_prefers_connection_row(with_key):
    conn = _connection(password_enc=encrypt_value("real-pw", bytes.fromhex(KEY_HEX)))
    ds = SimpleNamespace(
        source_type="postgresql",
        connection_config={"table_name": "orders", "db_name": "sales"},
        connection=conn,
    )
    creds = credentials_for(ds)
    assert creds["password"] == "real-pw"
    assert creds["table_name"] == "orders"
    assert creds["host"] == "10.0.0.5"


def test_credentials_for_falls_back_to_legacy_config(with_key):
    ds = SimpleNamespace(
        source_type="postgresql",
        connection_config={"host": "h", "port": 5432, "db_name": "d", "username": "u",
                           "password": encrypt_value("legacy", bytes.fromhex(KEY_HEX)),
                           "table_name": "t"},
        connection=None,
    )
    creds = credentials_for(ds)
    assert creds["password"] == "legacy"
    assert creds["db_name"] == "d"


def test_connection_response_never_leaks_password(with_key):
    conn = _connection(password_enc=encrypt_value("topsecret", bytes.fromhex(KEY_HEX)),
                       ssl_ca_enc=encrypt_value("PEM", bytes.fromhex(KEY_HEX)))
    payload = ConnectionResponse.from_connection(conn, datasource_count=3)
    dumped = str(payload.model_dump(mode="json", by_alias=True))
    assert "topsecret" not in dumped
    assert payload.has_password is True
    assert payload.ssl_ca_configured is True
    assert payload.datasource_count == 3


def test_datasource_name_for_table():
    assert DataSourceService.datasource_name_for_table("ecommerce_orders") == "ecommerce orders"
    assert DataSourceService.datasource_name_for_table("") == "未命名表"


# ── F2：TLS ─────────────────────────────────────────────────────────

def test_attach_statements_include_sslmode():
    secret, attach = build_attach_statements(
        {"host": "h", "port": 5432, "username": "u", "password": "p", "db_name": "d",
         "ssl_mode": "require"},
        "sch", "postgresql",
    )
    assert "SSLMODE 'require'" in secret
    assert "READ_ONLY" in attach


def test_attach_statements_include_ca_only_for_verify_modes():
    secret, _ = build_attach_statements(
        {"host": "h", "port": 5432, "username": "u", "password": "p", "db_name": "d",
         "ssl_mode": "verify-full", "ssl_ca_path": "/tmp/ca.pem"},
        "sch", "postgresql",
    )
    assert "SSLMODE 'verify-full'" in secret
    assert "SSLROOTCERT '/tmp/ca.pem'" in secret
    secret2, _ = build_attach_statements(
        {"host": "h", "port": 5432, "username": "u", "password": "p", "db_name": "d",
         "ssl_mode": "require", "ssl_ca_path": "/tmp/ca.pem"},
        "sch", "postgresql",
    )
    assert "SSLROOTCERT" not in secret2


def test_mysql_ssl_mode_is_mapped():
    secret, _ = build_attach_statements(
        {"host": "h", "port": 3306, "username": "u", "password": "p", "db_name": "d",
         "ssl_mode": "verify-full"},
        "sch", "mysql",
    )
    assert "SSL_MODE 'verify_identity'" in secret


def test_invalid_ssl_mode_rejected():
    with pytest.raises(connection_guard.ConnectionValidationError):
        connection_guard.validate_ssl("bogus", None)


def test_ca_must_be_pem():
    with pytest.raises(connection_guard.ConnectionValidationError):
        connection_guard.validate_ssl("verify-ca", "not-a-pem")


def test_ca_materialized_to_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TLS_CA_DIR", str(tmp_path / "tls"), raising=False)
    pem = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
    path = materialize_ca(uuid.uuid4(), pem)
    assert path and open(path, encoding="utf-8").read() == pem


# ── F3：敏感列 ──────────────────────────────────────────────────────

def test_detect_sensitive_by_name():
    assert sensitive_columns.detect_sensitive("password_hash")
    assert sensitive_columns.detect_sensitive("id_card_no")
    assert sensitive_columns.detect_sensitive("手机号")
    assert sensitive_columns.detect_sensitive("amount") is None


def test_mark_fields_drops_samples_and_keeps_user_flag():
    fields = [
        {"name": "password_hash", "sample": ["$2b$12$xx"]},
        {"name": "email", "sample": ["a@b.c"]},
        {"name": "token", "sensitive": False, "sample": ["t"]},  # 用户手工关掉
    ]
    marked = sensitive_columns.mark_fields(fields)
    by_name = {f["name"]: f for f in marked}
    assert by_name["password_hash"]["sensitive"] is True
    assert by_name["password_hash"]["sample"] == []
    assert by_name["email"]["sensitive"] is False
    assert by_name["token"]["sensitive"] is False  # 手工值优先


def test_mask_rows_and_reference_detection():
    marked = sensitive_columns.mark_fields([
        {"name": "password_hash"}, {"name": "amount"},
    ])
    names = sensitive_columns.sensitive_names(marked)
    rows = sensitive_columns.mask_rows(["password_hash", "amount"], [["x", 1]], names)
    assert rows == [["***", 1]]
    assert sensitive_columns.referenced_sensitive(
        'SELECT "password_hash" FROM t', names) == ["password_hash"]
    assert sensitive_columns.referenced_sensitive("SELECT amount FROM t", names) == []


@pytest.mark.asyncio
async def test_list_fields_hides_sensitive_columns():
    from app.services.agent_tools import ListFieldsTool

    datasource = SimpleNamespace(
        id=uuid.uuid4(), name="users",
        schema_meta={"fields": [
            {"name": "id", "data_type": "UUID", "sample": ["1"]},
            {"name": "password_hash", "data_type": "VARCHAR", "sensitive": True, "sample": ["x"]},
        ]},
    )

    class _Result:
        def scalar_one_or_none(self):
            return datasource

    class _DB:
        async def execute(self, *_a, **_k):
            return _Result()

    out = await ListFieldsTool().execute(
        datasource_id=str(datasource.id), user_id=str(uuid.uuid4()), db_session=_DB()
    )
    import json as _json

    payload = _json.loads(out)
    assert "password_hash" not in payload["columns"]
    assert payload["sensitive_columns"] == ["password_hash"]
    assert "sensitive_hint" in payload


# ── F4：轮换与健康 ─────────────────────────────────────────────────

class _FakeDB:
    def __init__(self, connection):
        self.connection = connection
        self.flushed = 0
        self.committed = 0

    async def execute(self, *_a, **_k):
        connection = self.connection

        class _Result:
            def scalar_one_or_none(self_inner):
                return connection

        return _Result()

    async def flush(self):
        self.flushed += 1

    async def refresh(self, _obj=None):
        return None

    async def delete(self, _obj=None):
        return None

    async def commit(self):
        self.committed += 1


@pytest.mark.asyncio
async def test_check_health_marks_failure(monkeypatch, with_key):
    connection = _connection(password_enc=encrypt_value("pw", bytes.fromhex(KEY_HEX)))
    service = ConnectionService(_FakeDB(connection))

    async def fake_probe(conn):
        return False, "connection refused"

    monkeypatch.setattr(service, "probe", fake_probe)
    payload = await service.check_health(connection.id, connection.user_id, force=True)
    assert payload["status"] == "failed"
    assert "connection refused" in payload["error"]
    assert connection.status == "disconnected"


@pytest.mark.asyncio
async def test_rotate_secret_verifies_before_writing(monkeypatch, with_key):
    connection = _connection(password_enc=encrypt_value("old", bytes.fromhex(KEY_HEX)))
    service = ConnectionService(_FakeDB(connection))

    async def failing_probe(_conn, _creds):
        return False, "password authentication failed"

    monkeypatch.setattr(service, "_probe_creds", failing_probe)
    with pytest.raises(connection_guard.ConnectionValidationError):
        await service.rotate_secret(connection.id, connection.user_id, "wrong")
    # 旧凭据不能被覆盖
    assert connection_credentials(connection)["password"] == "old"


@pytest.mark.asyncio
async def test_rotate_secret_updates_ciphertext(monkeypatch, with_key):
    connection = _connection(password_enc=encrypt_value("old", bytes.fromhex(KEY_HEX)))
    service = ConnectionService(_FakeDB(connection))

    async def ok_probe(_conn, _creds):
        return True, None

    async def no_children(_conn, force=True):
        return {"ok": [], "failed": []}

    monkeypatch.setattr(service, "_probe_creds", ok_probe)
    monkeypatch.setattr(service, "reattach_children", no_children)
    result = await service.rotate_secret(connection.id, connection.user_id, "new-pw")
    assert result["verified"] is True
    assert is_encrypted(connection.password_enc)
    assert connection_credentials(connection)["password"] == "new-pw"
    assert connection.health_status == "ok"


@pytest.mark.asyncio
async def test_rotate_secret_missing_connection(monkeypatch, with_key):
    service = ConnectionService(_FakeDB(None))
    with pytest.raises(ConnectionServiceError):
        await service.rotate_secret(uuid.uuid4(), uuid.uuid4(), "x")
