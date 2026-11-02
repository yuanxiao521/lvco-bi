# -*- coding: utf-8 -*-
"""数据源连接安全改造的回归测试。

覆盖四层防线：
  1. 凭据加密（AES-256-GCM 版本化信封 + 兼容旧格式 + fail-closed）；
  2. ATTACH 语句（密码进 DuckDB SECRET，不进连接串；字面量转义；只读附加）；
  3. 连接参数与网络边界（SSRF / 非法字符 / 端口 / 白名单）；
  4. 对外暴露面（API 响应脱敏、错误信息脱敏）。
"""
import uuid
from base64 import b64encode
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from app.config import settings
from app.connectors.attach_sql import build_attach_statements, table_ref
from app.models.datasource import DatasourceStatus, SourceType
from app.schemas import DataSourceResponse
from app.services import connection_guard
from app.services.datasource_connection import (
    DatasourceConnectionError,
    canonicalize,
    ensure_attached,
    resolve_credentials,
    sanitize_error,
    seal_connection_config,
)
from app.utils.crypto import (
    CredentialDecryptError,
    CredentialKeyMissingError,
    decrypt_value,
    encrypt_value,
    is_encrypted,
    seal_secret,
)

KEY_HEX = "11" * 32


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "db_encryption_key", KEY_HEX, raising=False)
    return bytes.fromhex(KEY_HEX)


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.setattr(settings, "db_encryption_key", None, raising=False)


# ── 1. 凭据加密 ──────────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip(with_key):
    token = encrypt_value("p@ss w'ord; --", with_key)
    assert is_encrypted(token)
    assert token.startswith("enc:v2:")
    assert decrypt_value(token, with_key) == "p@ss w'ord; --"


def test_ciphertext_is_randomized(with_key):
    assert encrypt_value("same", with_key) != encrypt_value("same", with_key)


def test_tampered_ciphertext_is_rejected(with_key):
    token = encrypt_value("secret", with_key)
    raw = bytearray(token[len("enc:v2:"):].encode())
    raw[-2] = ord("A") if raw[-2] != ord("A") else ord("B")
    tampered = "enc:v2:" + raw.decode()
    with pytest.raises(CredentialDecryptError):
        decrypt_value(tampered, with_key)


def test_wrong_key_is_reported_not_silently_wrong(with_key):
    token = encrypt_value("secret", with_key)
    other = bytes.fromhex("22" * 32)
    with pytest.raises(CredentialDecryptError):
        decrypt_value(token, other)


def test_legacy_cbc_ciphertext_still_decrypts(with_key):
    iv = b"0" * 16
    legacy = b64encode(iv + AES.new(with_key, AES.MODE_CBC, iv).encrypt(pad(b"legacy-pw", 16))).decode()
    assert not is_encrypted(legacy)
    assert decrypt_value(legacy, with_key) == "legacy-pw"


def test_plaintext_password_is_tolerated_for_migration(with_key):
    assert decrypt_value("plain-pw", with_key) == "plain-pw"


def test_seal_secret_fails_closed(without_key):
    with pytest.raises(CredentialKeyMissingError):
        seal_secret("plain-pw")


def test_seal_secret_with_key(with_key):
    sealed = seal_secret("plain-pw")
    assert is_encrypted(sealed)


def test_resolve_credentials_handles_mixed_key_names(with_key):
    cfg = {"host": "h", "port": "5433", "database": "d", "user": "u",
           "password": encrypt_value("pw", with_key)}
    resolved = resolve_credentials(cfg, SourceType.postgresql)
    assert resolved["port"] == 5433
    assert resolved["db_name"] == "d"
    assert resolved["username"] == "u"
    assert resolved["password"] == "pw"


def test_canonicalize_defaults_port():
    assert canonicalize({}, SourceType.postgresql)["port"] == 5432
    assert canonicalize({}, SourceType.mysql)["port"] == 3306


def test_seal_connection_config_validates_and_encrypts(with_key):
    stored = seal_connection_config(
        host="10.0.0.5", port=5432, db_name="sales", username="ro",
        password="pw", table_name="orders", source_type="postgresql",
    )
    assert is_encrypted(stored["password"])
    assert stored["secret_format"] == "v2"
    assert stored["db_name"] == "sales"


def test_seal_connection_config_rejects_bad_host(with_key):
    with pytest.raises(connection_guard.ConnectionValidationError):
        seal_connection_config(
            host="169.254.169.254", port=5432, db_name="d", username="u",
            password="pw", source_type="postgresql",
        )


# ── 2. ATTACH 语句 ───────────────────────────────────────────────────

def test_password_goes_to_secret_not_attach_statement():
    statements = build_attach_statements(
        {"host": "10.0.0.5", "port": 5432, "username": "ro",
         "password": "TopSecret123", "db_name": "sales"},
        "sales_abc",
        "postgresql",
    )
    secret_stmt, attach_stmt = statements
    assert "TopSecret123" in secret_stmt
    assert "TopSecret123" not in attach_stmt
    assert "SECRET" in attach_stmt and "READ_ONLY" in attach_stmt
    assert "PASSWORD" not in attach_stmt


def test_quotes_are_escaped():
    secret_stmt, attach_stmt = build_attach_statements(
        {"host": "h", "port": 5432, "username": "u",
         "password": "p'a ss; drop", "db_name": "d"},
        "sch",
        "postgresql",
    )
    assert "PASSWORD 'p''a ss; drop'" in secret_stmt
    assert attach_stmt == "ATTACH '' AS \"sch\" (TYPE postgres, SECRET lvco_ds_" + secret_stmt.split("lvco_ds_")[1].split(" ")[0] + ", READ_ONLY)"


def test_table_ref_by_source_type():
    assert table_ref("sch", "postgresql", "orders") == '"sch".public."orders"'
    assert table_ref("sch", "mysql", "orders") == '"sch"."orders"'
    assert table_ref("sch", "csv", "") == '"sch".data'


def test_ensure_attached_skips_local_sources():
    ds = SimpleNamespace(source_type=SourceType.csv, connection_config=None)
    assert ensure_attached(ds, "sch_local") is False


def test_ensure_attached_sanitizes_connection_error(monkeypatch, with_key):
    from app.services import datasource_connection as dc

    def boom(schema_name, statements, force=False):
        raise RuntimeError("failed for host=h password=TopSecret123 dbname=d")

    monkeypatch.setattr(dc.duckdb_client, "ensure_attached", boom)
    ds = SimpleNamespace(
        source_type=SourceType.postgresql,
        connection_config={
            "host": "h", "port": 5432, "db_name": "d", "username": "u",
            "password": encrypt_value("TopSecret123", with_key),
        },
    )
    with pytest.raises(DatasourceConnectionError) as excinfo:
        ensure_attached(ds, "sch_x")
    message = str(excinfo.value)
    assert "TopSecret123" not in message
    assert "password=***" in message


# ── 3. 连接参数与网络边界 ────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "169.254.169.254",
    "100.100.100.200",
    "0.0.0.0",
    "255.255.255.255",
    "239.1.1.1",
])
def test_ssrf_hosts_are_blocked(host):
    with pytest.raises(connection_guard.ConnectionValidationError):
        connection_guard.validate_host(host)


@pytest.mark.parametrize("host", [
    "127.0.0.1; DROP TABLE x",
    "postgres://user@host",
    "host name",
])
def test_illegal_hosts_are_blocked(host):
    with pytest.raises(connection_guard.ConnectionValidationError):
        connection_guard.validate_host(host)


def test_private_host_allowed_by_default():
    assert connection_guard.validate_host("10.0.0.5") == "10.0.0.5"
    assert connection_guard.validate_host("db.internal") == "db.internal"


def test_private_host_can_be_disallowed(monkeypatch):
    monkeypatch.setattr(settings, "DB_ALLOW_PRIVATE_HOSTS", False, raising=False)
    with pytest.raises(connection_guard.ConnectionValidationError):
        connection_guard.validate_host("192.168.1.10")


def test_allowlist_restricts_hosts(monkeypatch):
    monkeypatch.setattr(settings, "DB_ALLOWED_HOSTS", "corp-db.example.com", raising=False)
    assert connection_guard.validate_host("corp-db.example.com")
    with pytest.raises(connection_guard.ConnectionValidationError):
        connection_guard.validate_host("8.8.8.8")


def test_port_validation():
    assert connection_guard.validate_port(None, 5432) == 5432
    assert connection_guard.validate_port("3306", 5432) == 3306
    for bad in (0, 70000, "abc"):
        with pytest.raises(connection_guard.ConnectionValidationError):
            connection_guard.validate_port(bad, 5432)


# ── 4. 对外暴露面 ───────────────────────────────────────────────────

def _fake_datasource(connection_config):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="pg",
        description=None,
        source_type=SourceType.postgresql,
        connection_config=connection_config,
        file_path=None,
        schema_meta={"fields": []},
        status=DatasourceStatus.connected,
        size_bytes=0,
        row_count=0,
        last_synced_at=now,
        created_at=now,
        updated_at=now,
    )


def test_response_never_returns_password():
    ds = _fake_datasource({
        "host": "10.0.0.5", "port": 5432, "db_name": "sales", "username": "ro",
        "password": "plaintext-should-not-leak", "secret_format": "v2",
    })
    payload = DataSourceResponse.model_validate(ds).model_dump(mode="json", by_alias=True)
    dumped = str(payload)
    assert "plaintext-should-not-leak" not in dumped
    keys = {k.lower() for k in payload["connectionConfig"].keys()}
    assert "password" not in keys
    # 内嵌 dict 不参与 alias 转换，键名与 connection_config 内部一致用小写下划线
    assert payload["connectionConfig"]["has_password"] is True
    assert payload["connectionConfig"]["host"] == "10.0.0.5"


def test_sanitize_error_strips_password_forms():
    text = (
        'IO Error: connection to "h" failed password=TopSecret123; '
        "url postgresql://lvco:TopSecret123@h:5432/db"
    )
    cleaned = sanitize_error(text, secrets=["TopSecret123"])
    assert "TopSecret123" not in cleaned
    assert "***" in cleaned


def test_sanitize_error_truncates():
    assert len(sanitize_error("x" * 1000, limit=50)) == 50
