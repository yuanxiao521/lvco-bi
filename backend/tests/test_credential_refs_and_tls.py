# -*- coding: utf-8 -*-
"""A+B 回归：传输层强制加密 / 服务端凭据引用。

A（REQUIRE_HTTPS）：明文请求一律 403，带 X-Forwarded-Proto: https 才放行；
B（credential_ref）：env:/file: 引用解析、越权与自身密钥防护、故障可读、
  引用型连接禁止在页面轮换密码。
"""
import os
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import credential_refs
from app.services.connection_guard import ConnectionValidationError
from app.services.connection_service import ConnectionService
from app.services.datasource_connection import (
    build_connection_columns,
    connection_credentials,
)

REF_ENV = "LVCO_TEST_PG_PASSWORD"


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(settings, "db_encryption_key", "44" * 32, raising=False)


@pytest.fixture
def clean_ref_settings(monkeypatch):
    monkeypatch.setattr(settings, "DB_SECRET_ENV_ALLOWLIST", "", raising=False)
    monkeypatch.setattr(settings, "DB_SECRET_FILE_ROOTS", "/run/secrets,./data/secrets", raising=False)


# ── B：引用解析 ─────────────────────────────────────────────────────

def test_parse_ref_rejects_unknown_scheme(clean_ref_settings):
    with pytest.raises(credential_refs.CredentialRefError):
        credential_refs.parse_ref("vault:secret/data/pg")
    with pytest.raises(credential_refs.CredentialRefError):
        credential_refs.parse_ref("just-a-plain-password")


def test_env_ref_rejects_app_own_secrets(clean_ref_settings):
    for name in ("JWT_SECRET_KEY", "DB_ENCRYPTION_KEY", "OPENAI_API_KEY"):
        with pytest.raises(credential_refs.CredentialRefError):
            credential_refs.validate_ref(f"env:{name}")


def test_env_ref_allowlist(clean_ref_settings, monkeypatch):
    monkeypatch.setattr(settings, "DB_SECRET_ENV_ALLOWLIST", "OTHER_PASSWORD", raising=False)
    with pytest.raises(credential_refs.CredentialRefError):
        credential_refs.validate_ref("env:LVCO_TEST_PG_PASSWORD")
    assert credential_refs.validate_ref("env:OTHER_PASSWORD") == "env:OTHER_PASSWORD"


def test_env_ref_resolution(clean_ref_settings, monkeypatch):
    monkeypatch.setenv(REF_ENV, "s3cret-from-env")
    assert credential_refs.resolve_ref(f"env:{REF_ENV}") == "s3cret-from-env"


def test_env_ref_missing_is_actionable(clean_ref_settings, monkeypatch):
    monkeypatch.delenv(REF_ENV, raising=False)
    with pytest.raises(credential_refs.CredentialRefError) as excinfo:
        credential_refs.resolve_ref(f"env:{REF_ENV}")
    assert REF_ENV in str(excinfo.value)
    assert "未设置" in str(excinfo.value)


def test_file_ref_must_be_absolute(clean_ref_settings):
    with pytest.raises(credential_refs.CredentialRefError):
        credential_refs.validate_ref("file:relative/path")


def test_file_ref_denied_outside_roots(tmp_path, monkeypatch):
    allowed = tmp_path / "secrets"
    allowed.mkdir()
    monkeypatch.setattr(settings, "DB_SECRET_FILE_ROOTS", str(allowed), raising=False)
    outside = tmp_path / "other.txt"
    outside.write_text("evil", encoding="utf-8")
    with pytest.raises(credential_refs.CredentialRefError):
        credential_refs.validate_ref(f"file:{outside}")
    # 目录内合法
    inside = allowed / "pg_pw"
    inside.write_text("pw-from-file\n", encoding="utf-8")
    assert credential_refs.validate_ref(f"file:{inside}") == f"file:{inside}"
    assert credential_refs.resolve_ref(f"file:{inside}") == "pw-from-file"


def test_file_ref_traversal_is_blocked(tmp_path, monkeypatch):
    allowed = tmp_path / "secrets"
    allowed.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("evil", encoding="utf-8")
    monkeypatch.setattr(settings, "DB_SECRET_FILE_ROOTS", str(allowed), raising=False)
    with pytest.raises(credential_refs.CredentialRefError):
        credential_refs.validate_ref(f"file:{allowed}/../outside.txt")


# ── B：连接层融合 ──────────────────────────────────────────────────

def test_build_columns_with_ref_keeps_no_ciphertext(with_key, clean_ref_settings, monkeypatch):
    monkeypatch.setenv(REF_ENV, "pw")
    columns = build_connection_columns(
        host="10.0.0.5", port=5432, db_name="d", username="u",
        password=None, credential_ref=f"env:{REF_ENV}",
    )
    assert columns["credential_ref"] == f"env:{REF_ENV}"
    assert columns["password_enc"] is None


def test_connection_credentials_resolves_ref(with_key, clean_ref_settings, monkeypatch):
    monkeypatch.setenv(REF_ENV, "resolved-pw")
    conn = SimpleNamespace(
        host="h", port=5432, db_name="d", username="u",
        password_enc=None, credential_ref=f"env:{REF_ENV}", ssl_mode="prefer", ssl_ca_enc=None,
    )
    creds = connection_credentials(conn)
    assert creds["password"] == "resolved-pw"


def test_connection_credentials_ref_failure_is_readable(with_key, clean_ref_settings, monkeypatch):
    monkeypatch.delenv("MISSING_PW_VAR", raising=False)
    conn = SimpleNamespace(
        host="h", port=5432, db_name="d", username="u",
        password_enc=None, credential_ref="env:MISSING_PW_VAR", ssl_mode="prefer", ssl_ca_enc=None,
    )
    from app.services.datasource_connection import DatasourceConnectionError

    with pytest.raises(DatasourceConnectionError) as excinfo:
        connection_credentials(conn)
    assert "MISSING_PW_VAR" in str(excinfo.value)


# ── B：服务层（创建/轮换/切换） ────────────────────────────────────

class _FakeDB:
    def __init__(self, connection=None):
        self.connection = connection

    async def execute(self, *_a, **_k):
        connection = self.connection

        class _Result:
            def scalar_one_or_none(self_inner):
                return connection

        return _Result()

    async def flush(self):
        return None

    async def refresh(self, _obj=None):
        return None

    async def delete(self, _obj=None):
        return None


@pytest.mark.asyncio
async def test_create_requires_password_or_ref(with_key, clean_ref_settings):
    service = ConnectionService(_FakeDB())
    with pytest.raises(ConnectionValidationError):
        await service.create(
            uuid.uuid4(), name="x", source_type="postgresql", host="10.0.0.5",
            port=5432, db_name="d", username="u", password=None,
        )


@pytest.mark.asyncio
async def test_create_with_ref_fails_fast_when_env_missing(with_key, clean_ref_settings, monkeypatch):
    monkeypatch.delenv("NOPE_PW", raising=False)
    service = ConnectionService(_FakeDB())
    with pytest.raises(ConnectionValidationError) as excinfo:
        await service.create(
            uuid.uuid4(), name="x", source_type="postgresql", host="10.0.0.5",
            port=5432, db_name="d", username="u", credential_ref="env:NOPE_PW",
        )
    assert "NOPE_PW" in str(excinfo.value)


@pytest.mark.asyncio
async def test_rotate_secret_rejected_for_reference_connection(with_key, clean_ref_settings):
    connection = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), credential_ref="env:LVCO_X",
        password_enc=None, datasources=[],
    )
    service = ConnectionService(_FakeDB(connection))
    with pytest.raises(ConnectionValidationError) as excinfo:
        await service.rotate_secret(connection.id, connection.user_id, "new")
    assert "服务端凭据引用" in str(excinfo.value)


@pytest.mark.asyncio
async def test_update_switch_between_manual_and_reference(with_key, clean_ref_settings, monkeypatch):
    monkeypatch.setenv(REF_ENV, "pw")
    connection = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), credential_ref=None,
        password_enc="enc:v2:old", ssl_mode="prefer", ssl_ca_enc=None,
        datasources=[], health_status=None, health_error=None,
    )
    service = ConnectionService(_FakeDB(connection))

    async def noop_reattach(_conn, force=True):
        return {"ok": [], "failed": []}

    monkeypatch.setattr(service, "reattach_children", noop_reattach)

    await service.update(connection.id, connection.user_id, credential_source="reference",
                         credential_ref=f"env:{REF_ENV}")
    assert connection.credential_ref == f"env:{REF_ENV}"
    assert connection.password_enc is None

    await service.update(connection.id, connection.user_id, credential_source="manual",
                         password="new-pw")
    assert connection.password_enc and connection.credential_ref is None


# ── A：传输层强制加密 ─────────────────────────────────────────────

def test_https_required_blocks_plaintext(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "REQUIRE_HTTPS", True, raising=False)
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200  # 健康检查豁免
    resp = client.get("/api/v1/datasources")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "HTTPS_REQUIRED"
    # 代理声明 https 后放行（认证失败返回 401/403 之外的状态也算通过网关层）
    resp = client.get("/api/v1/datasources", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code != 403


def test_https_required_off_allows_plaintext(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "REQUIRE_HTTPS", False, raising=False)
    client = TestClient(app)
    resp = client.get("/api/v1/datasources")
    assert resp.status_code != 403
