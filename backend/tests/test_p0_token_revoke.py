"""P0 止血：令牌吊销（登出 / 改密）+ is_active 校验。

覆盖目标（对应 task-11 验收）：
1. 登出后**旧 token 立刻 401**（不用等 30 分钟过期）
2. 正常有效 token → 200
3. is_active=false → 403
4. 兼容性：升级前签发的 token（没有 token_version claim）**不会**被判失效
5. 旁路：refresh token 也必须被吊销（否则 7 天内可无限续命）
6. 改密：旧 token 失效 + 新令牌可用

为什么不用真实数据库 / 不启动 `app.main:app`：
- `main.py` 的 startup 会连 Postgres + Redis 并起调度器，单测环境不可用；
- 本文件的目的是验证**鉴权逻辑本身**（deps.auth 校验链 + 路由行为），
  因此用最小 FastAPI 应用挂载真实 router，把 `get_db` 覆盖成一个只认识
  一个内存 User 的假会话——被测试的仍是生产代码，只有持久化被替换。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.api.v1 import auth as auth_module
from app.config import settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
)
from app.models.user import User, UserRole

API = "/api/v1/auth"
OLD_PASSWORD = "OldPass123"
NEW_PASSWORD = "NewPass456"


# ── 假会话：只认识一个 User 对象 ─────────────────────────────────────────────


class _FakeResult:
    def __init__(self, user: User | None) -> None:
        self._user = user

    def scalar_one_or_none(self) -> User | None:
        return self._user


class FakeSession:
    """最小 AsyncSession 替身：execute 恒返回同一个 user，commit 计数。"""

    def __init__(self, user: User | None) -> None:
        self.user = user
        self.commits = 0

    async def execute(self, stmt):  # noqa: ANN001 - 只关心"查到了谁"
        return _FakeResult(self.user)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:  # noqa: ANN001
        return None

    def add(self, obj) -> None:  # noqa: ANN001
        return None


def _make_user(*, version: int = 0, is_active: bool = True) -> User:
    """构造一个未落库的 User（真实 ORM 对象，字段与生产一致）。"""
    return User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash=hash_password(OLD_PASSWORD),
        display_name="Tom",
        role=UserRole.editor,
        token_version=version,
        is_active=is_active,
    )


def _client(user: User | None) -> tuple[TestClient, FakeSession]:
    db = FakeSession(user)
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/api/v1")

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app), db


def _access_token(user: User, *, version: int | None = None) -> str:
    """按给定版本签发 access token（默认用用户当前版本）。"""
    v = (user.token_version or 0) if version is None else version
    return create_access_token(str(user.id), {"email": user.email}, token_version=v)


def _legacy_access_token(user: User) -> str:
    """模拟**升级前**签发的 token：没有 token_version claim。

    注意不能用 create_access_token 造——它现在总会写 claim（默认 0）。
    这里手工签，才是真正的"老 token"。
    """
    claims = {
        "sub": str(user.id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "type": "access",
    }
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 1. 基线：有效 token 正常通过 ────────────────────────────────────────────


def test_valid_token_returns_200():
    user = _make_user()
    client, _db = _client(user)
    resp = client.post(f"{API}/logout", headers=_auth(_access_token(user)))
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


# ── 2. 核心：登出后旧 token 立刻 401 ────────────────────────────────────────


def test_logout_revokes_old_access_token():
    user = _make_user()
    client, db = _client(user)
    old_token = _access_token(user)

    first = client.post(f"{API}/logout", headers=_auth(old_token))
    assert first.status_code == 200, first.text
    assert db.commits == 1, "登出必须把 token_version 落库"
    assert user.token_version == 1

    # 同一枚 token 再用：立刻 401，而不是等 30 分钟过期
    second = client.post(f"{API}/logout", headers=_auth(old_token))
    assert second.status_code == 401, second.text
    assert second.json()["detail"]["code"] == "UNAUTHORIZED"


def test_version_mismatch_gives_401():
    """等价于"登出之后"的状态：DB 版本比 token 里的新。"""
    user = _make_user(version=1)
    client, _db = _client(user)
    resp = client.post(f"{API}/logout", headers=_auth(_access_token(user, version=0)))
    assert resp.status_code == 401, resp.text


# ── 3. 账号停用 → 403 ───────────────────────────────────────────────────────


def test_inactive_user_gets_403():
    user = _make_user(is_active=False)
    client, _db = _client(user)
    resp = client.post(f"{API}/logout", headers=_auth(_access_token(user)))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


# ── 4. 兼容性：老 token（无 claim）不踢人 ───────────────────────────────────


def test_legacy_token_without_version_claim_is_accepted():
    user = _make_user(version=0)
    client, _db = _client(user)
    resp = client.post(f"{API}/logout", headers=_auth(_legacy_access_token(user)))
    assert resp.status_code == 200, resp.text


def test_legacy_token_rejected_after_logout():
    """老 token 也不能绕过吊销：登出把 DB 版本推到 1，老 token（视作 0）就该 401。"""
    user = _make_user(version=0)
    client, _db = _client(user)
    legacy = _legacy_access_token(user)
    assert client.post(f"{API}/logout", headers=_auth(legacy)).status_code == 200
    assert client.post(f"{API}/logout", headers=_auth(legacy)).status_code == 401


# ── 5. 旁路：refresh token 也必须被吊销 ─────────────────────────────────────


def test_refresh_happy_path_issues_versioned_access_token():
    user = _make_user(version=0)
    client, _db = _client(user)
    refresh = create_refresh_token(str(user.id), token_version=0)

    resp = client.post(f"{API}/refresh", json={"refreshToken": refresh})
    assert resp.status_code == 200, resp.text
    new_access = resp.json()["data"]["accessToken"]
    assert decode_token(new_access)["token_version"] == 0
    # 新 access token 可用
    assert client.post(f"{API}/logout", headers=_auth(new_access)).status_code == 200


def test_refresh_rejected_after_logout():
    """登出后，7 天有效的 refresh token 不能继续换 access token。"""
    user = _make_user(version=0)
    client, _db = _client(user)
    refresh = create_refresh_token(str(user.id), token_version=0)
    assert client.post(f"{API}/logout", headers=_auth(_access_token(user))).status_code == 200

    resp = client.post(f"{API}/refresh", json={"refreshToken": refresh})
    assert resp.status_code == 401, resp.text


def test_refresh_rejected_for_inactive_user():
    user = _make_user(is_active=False)
    client, _db = _client(user)
    refresh = create_refresh_token(str(user.id), token_version=0)
    resp = client.post(f"{API}/refresh", json={"refreshToken": refresh})
    assert resp.status_code == 403, resp.text


def test_refresh_rejects_unknown_user():
    """原实现只校验 token 签名，用户被删掉后仍能换 access token——这里守住。"""
    user = _make_user()
    client, _db = _client(None)  # 库里查不到人
    refresh = create_refresh_token(str(user.id), token_version=0)
    resp = client.post(f"{API}/refresh", json={"refreshToken": refresh})
    assert resp.status_code == 401, resp.text


# ── 6. 改密：吊销旧令牌 + 发新令牌 ──────────────────────────────────────────


def test_change_password_bumps_version_and_revokes_old_token():
    user = _make_user(version=0)
    client, db = _client(user)
    old_token = _access_token(user)

    resp = client.post(
        f"{API}/change-password",
        headers=_auth(old_token),
        json={"oldPassword": OLD_PASSWORD, "newPassword": NEW_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    assert db.commits == 1
    assert user.token_version == 1, "改密必须吊销该账号全部旧令牌"

    # 旧 token 失效
    assert client.post(f"{API}/logout", headers=_auth(old_token)).status_code == 401
    # 响应里带回的新 token 可用（当前设备无感续期）
    new_access = resp.json()["data"]["accessToken"]
    assert decode_token(new_access)["token_version"] == 1
    assert client.post(f"{API}/logout", headers=_auth(new_access)).status_code == 200


def test_change_password_wrong_old_password_does_not_bump():
    user = _make_user(version=0)
    client, db = _client(user)
    resp = client.post(
        f"{API}/change-password",
        headers=_auth(_access_token(user)),
        json={"oldPassword": "wrong-old", "newPassword": NEW_PASSWORD},
    )
    assert resp.status_code == 400, resp.text
    assert user.token_version == 0
    assert db.commits == 0
    # 旧 token 仍然可用（改密失败不应造成掉线）
    assert client.post(f"{API}/logout", headers=_auth(_access_token(user))).status_code == 200


# ── 7. 签发链路本身：login 出来的 token 必须带版本 ──────────────────────────


def test_issue_tokens_carries_current_version():
    """直接测路由层的签发助手：版本必须来自 user 行，否则登出后永久 401。"""
    user = _make_user(version=3)
    tokens = auth_module._issue_tokens(user)
    assert decode_token(tokens["accessToken"])["token_version"] == 3
    assert decode_token(tokens["refreshToken"])["token_version"] == 3
    assert tokens["expiresIn"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
