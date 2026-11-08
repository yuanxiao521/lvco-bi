# -*- coding: utf-8 -*-
"""P0 越权回归：审计日志 / 日志导出 / 用户列表必须 **admin only**。

背景（修复前）：
- `api/v1/audit.py` 的 `GET /audit/logs` 与 `GET /audit/logs/export` 只要求登录，
  原文注释即「任何登录用户可访问」，任何 viewer 都能拉全站用户的路径 / IP / UA；
- `api/v1/permissions.py` 的 `GET /permissions/users` 同样只要求登录，
  原文注释「任何登录用户可看」，可拿到全员 email / role 与各人资源数。

修复：三处均改为 `current_user.role != UserRole.admin` → 403，
响应结构与项目既有范式一致（`permissions.py:124-128`、`trash.py:148-152` 的
`detail={"code": "FORBIDDEN", "message": ...}`）。

mock 说明（诚实标注）：
- 需求要求「照抄 tests/ 里已有的 app.dependency_overrides 用法」，但本仓 **没有**
  任何 dependency_overrides 用法（全仓 grep 0 命中）。这里按 FastAPI 标准用法覆盖
  `get_current_user`（换角色）与 `get_db`（假会话），TestClient 的构造方式照抄仓库
  唯一既有的 TestClient 先例：`tests/test_credential_refs_and_tls.py:224-234`
  （直接 `TestClient(app)`，不进 lifespan，因此不会触发 startup 的 Redis/调度器）。
- 假会话只返回空结果集：三个接口的空数据分支足以区分 403/200，且不连真库。
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.core.database import get_db
from app.main import app
from app.models.user import User, UserRole

AUDIT_LOGS = "/api/v1/audit/logs"
AUDIT_EXPORT = "/api/v1/audit/logs/export"
PERMISSION_USERS = "/api/v1/permissions/users"


def _user(role: UserRole) -> User:
    """构造一个未入库的 User（只用到 id / role，不触碰 DB）。"""
    return User(
        id=uuid4(),
        email=f"{role.value}@example.com",
        display_name=role.value,
        password_hash="not-used-in-this-test",
        role=role,
    )


def _fake_db() -> MagicMock:
    """假会话：所有查询返回空结果集（分页/计数分支都能跑通）。"""
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = 0
    result.scalar_one_or_none.return_value = None
    result.all.return_value = []
    result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@contextmanager
def _client_as(role: UserRole):
    """以指定角色请求：覆盖鉴权依赖 + 数据依赖，退出时清理覆盖。"""

    async def _override_user():
        return _user(role)

    async def _override_db():
        yield _fake_db()

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _assert_forbidden(resp) -> None:
    assert resp.status_code == 403, f"期望 403，实际 {resp.status_code}: {resp.text[:300]}"
    detail = resp.json().get("detail") or {}
    assert detail.get("code") == "FORBIDDEN", f"403 响应结构应与既有范式一致: {detail}"
    assert detail.get("message"), "403 必须带可读 message"


# ── 1) viewer 拉审计日志 → 403 ────────────────────────────────────────────


def test_viewer_cannot_list_audit_logs():
    with _client_as(UserRole.viewer) as client:
        _assert_forbidden(client.get(AUDIT_LOGS))


# ── 2) viewer 导出审计日志 → 403 ──────────────────────────────────────────


def test_viewer_cannot_export_audit_logs():
    with _client_as(UserRole.viewer) as client:
        _assert_forbidden(client.get(AUDIT_EXPORT))


# ── 3) viewer 拉用户列表 → 403 ────────────────────────────────────────────


def test_viewer_cannot_list_users():
    with _client_as(UserRole.viewer) as client:
        _assert_forbidden(client.get(PERMISSION_USERS))


# ── 4) admin 两个入口都 200（证明 403 来自鉴权而不是接口坏了）────────────


def test_admin_can_access_audit_and_user_list():
    with _client_as(UserRole.admin) as client:
        logs = client.get(AUDIT_LOGS)
        assert logs.status_code == 200, f"admin 应能读审计日志: {logs.text[:300]}"
        assert "data" in logs.json()

        export = client.get(AUDIT_EXPORT)
        assert export.status_code == 200, f"admin 应能导出审计日志: {export.text[:300]}"
        assert export.headers.get("content-type", "").startswith("text/csv")

        users = client.get(PERMISSION_USERS)
        assert users.status_code == 200, f"admin 应能看用户列表: {users.text[:300]}"
        assert users.json().get("data", {}).get("items") == []
