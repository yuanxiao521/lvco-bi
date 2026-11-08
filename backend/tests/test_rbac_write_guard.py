"""P3-B 权限补强回归：viewer 只读 / 关闭自助注册 / 管理员停用账号。

分四层，全部不需要真实数据库：
  1) require_editor 单测：viewer 403（码与文案精确）/ editor、admin 放行 / role 是裸字符串也认；
  2) **棘轮**：遍历路由，断言"四个业务路由的每个写方法都挂了 require_editor"、
     "没有任何 GET 被挂"、"AI 额度入口都挂了、AI 只读 GET 都没挂"。
     新增写端点忘了挂闸 → 这个测试立刻变红（这是本文件的主要价值）；
  3) ASGI 行为：viewer POST 403 / editor POST 201 / viewer GET 不被拦 / AI 入口先 403 再谈配置；
  4) 开关与停用入口：ALLOW_PUBLIC_REGISTER=False → 403 REGISTER_DISABLED；
     PATCH /permissions/users/{id}/status 的 admin / 自己 / 最后一名 admin 三条护栏。

**未覆盖（诚实标注）**：
  - 没用真实 PG/真实登录态：所有身份都是 dependency_overrides 注入的假 User；
  - 没验证 role 在**非 HTTP 入口**（队列 worker、定时任务、内部 python 调用）是否也遵守只读；
  - metrics.py / insights.py / statistics.py 的写端点**不在本任务清单内**，未挂闸（已在汇报里列明）；
  - 前端未改：403 之后前端是否有可读提示未知。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, require_editor
from app.api.v1 import ai as ai_module
from app.api.v1.canvases import router as canvases_router
from app.api.v1.connections import router as connections_router
from app.api.v1.dashboards import router as dashboards_router
from app.api.v1.datasources import router as datasources_router
from app.api.v1.insights import router as insights_router
from app.api.v1.metrics import router as metrics_router
from app.api.v1.permissions import (
    UserStatusUpdateRequest,
    list_users,
    update_user_status,
)
from app.api.v1.reports import router as reports_router
from app.api.v1.statistics import statistics_router
from app.api.v1.trash import router as trash_router
# 导入 router 会执行"给 AI 额度入口追加依赖"的装配逻辑（必须在使用 ai_module.routes 断言前导入）
from app.api.v1.router import AI_EDITOR_ONLY_PATHS
from app.main import app
from app.models.user import UserRole

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 故意不挂闸的写端点（每条都写清理由）。棘轮测试会锁死"新增的写端点必须显式出现在这里"。
INTENTIONAL_UNGUARDED = {
    # 画布取数：纯 SELECT，viewer 打开画布时前端要靠它渲染，挂掉等于"只读=不可用"
    ("POST", "/canvases/{canvas_id}/query"),
    # 仪表盘刷新：只触发重算 + 更新 last_refreshed_at，不改变业务对象；语义不明确，留待产品定
    ("POST", "/dashboards/{dashboard_id}/refresh"),
    # 连接级健康探测：只读网络操作（不改配置、不动凭据）。与 datasources 的 GET /health 同语义，
    # 两处保持一致都按"只读"处理（Lead 原话既说"健康检查触发要挂"又说"不要挂 GET"，冲突时取后者）。
    ("POST", "/connections/{connection_id}/health"),
    # ── 只读统计（statistics.py）────────────────────────────────────────────
    # 这 6 个 POST 只是"用 JSON body 传参"的统计计算：全文件没有 db.add/commit/
    # insert/update/delete（已 grep 核对），只往 DuckDB 发 SELECT 并把结果算好返回。
    # 按 Lead 的"只读统计不要挂"处理；它们的 POST 是传参便利，不是写语义。
    ("POST", "/statistics/describe"),
    ("POST", "/statistics/correlation"),
    ("POST", "/statistics/ranking"),
    ("POST", "/statistics/summary"),
    ("POST", "/statistics/comparison"),
    ("POST", "/statistics/preview"),
    # ── 彻底删除 ────────────────────────────────────────────────────────────
    # trash.py:148 已有比 require_editor **更严格**的 admin-only 内联闸：
    # 挂上 require_editor 只会把 viewer 看到的提示从"彻底删除仅限管理员操作"
    # 降级成泛化的"只读账号不能执行该操作"，信息量更差，故不重复挂。
    # 该行为由 test_trash_permanent_delete_is_admin_only 单独盯住。
    ("DELETE", "/trash/{item_type}/{item_id}"),
}

# AI 路由里"写但不算额度"的会话簿记端点（建会话/改名/删会话/取消任务），不做编辑闸
AI_BOOKKEEPING_WRITES = {
    ("POST", "/ai/sessions"),
    ("PATCH", "/ai/sessions/{session_id}"),
    ("DELETE", "/ai/sessions/{session_id}"),
    ("POST", "/ai/sessions/{session_id}/cancel"),
}


# ── helpers ─────────────────────────────────────────────────────────────────


def _user(role, uid=None, *, is_active=True):
    return SimpleNamespace(
        id=uid or uuid.uuid4(),
        role=role,
        email="probe@example.invalid",
        display_name="probe",
        avatar_url=None,
        created_at=datetime.now(timezone.utc),
        is_active=is_active,
        token_version=0,
    )


def _has_editor_guard(route) -> bool:
    for dep in getattr(route, "dependencies", None) or []:
        if getattr(dep, "dependency", None) is require_editor:
            return True
    return False


_ALL_ROUTERS = (
    datasources_router, connections_router, canvases_router, dashboards_router,
    reports_router, metrics_router, insights_router, statistics_router, trash_router,
)


def _iter_routes(router):
    for route in router.routes:
        for method in (getattr(route, "methods", None) or set()):
            yield method.upper(), route


def _client(role, overrides: dict | None = None) -> TestClient:
    """按角色注入当前用户；测试结束由调用方清理 overrides。"""
    user = _user(role)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides.update(overrides or {})
    return TestClient(app)


def _cleanup_overrides() -> None:
    app.dependency_overrides.clear()


# ── 1) require_editor 单测 ──────────────────────────────────────────────────


def test_viewer_is_forbidden_with_exact_contract():
    with pytest.raises(HTTPException) as ei:
        require_editor(current_user=_user(UserRole.viewer))
    assert ei.value.status_code == 403
    assert ei.value.detail == {"code": "FORBIDDEN", "message": "只读账号不能执行该操作"}


@pytest.mark.parametrize("role", [UserRole.editor, UserRole.admin])
def test_editor_and_admin_pass_through(role):
    user = _user(role)
    assert require_editor(current_user=user) is user


def test_viewer_as_plain_string_still_forbidden():
    """role 理论上总是枚举，但旧数据/测试替身可能给字符串，两种情况都要挡住。"""
    with pytest.raises(HTTPException) as ei:
        require_editor(current_user=_user("viewer"))
    assert ei.value.status_code == 403


# ── 2) 棘轮：路由覆盖 ───────────────────────────────────────────────────────


def test_every_write_route_has_editor_guard():
    routers = {
        "datasources": datasources_router,
        "connections": connections_router,
        "canvases": canvases_router,
        "dashboards": dashboards_router,
        "reports": reports_router,
        "metrics": metrics_router,
        "insights": insights_router,
        "statistics": statistics_router,
        "trash": trash_router,
    }
    missing = []
    for name, router in routers.items():
        for method, route in _iter_routes(router):
            if method not in WRITE_METHODS:
                continue
            if (method, route.path) in INTENTIONAL_UNGUARDED:
                continue
            if not _has_editor_guard(route):
                missing.append(f"{name}: {method} {route.path}")
    assert not missing, "这些写端点没挂 require_editor（viewer 能写）: " + ", ".join(missing)


def test_no_get_route_is_guarded():
    """只读账号必须还能看，GET 一律不许挂 require_editor。"""
    guarded = []
    for router in _ALL_ROUTERS:
        for method, route in _iter_routes(router):
            if method == "GET" and _has_editor_guard(route):
                guarded.append(route.path)
    assert not guarded, f"这些 GET 被误挂了写闸: {guarded}"


def test_intentional_unguarded_list_is_accurate():
    """白名单不能腐烂：列进去的端点必须真的没挂，没列进去的必须挂了。"""
    by_path = {}
    for router in _ALL_ROUTERS:
        for method, route in _iter_routes(router):
            by_path[(method, route.path)] = _has_editor_guard(route)
    for key in INTENTIONAL_UNGUARDED:
        assert key in by_path, f"白名单里的端点已不存在（删掉这条）: {key}"
        assert by_path[key] is False, f"{key} 已经挂了闸，应从白名单移除"


def test_ai_llm_entrypoints_are_guarded():
    routes = {(m, r.path): r for m, r in _iter_routes(ai_module.router)}
    for path in AI_EDITOR_ONLY_PATHS:
        key = ("POST", path)
        assert key in routes, f"AI 额度入口清单里的路径不存在: {path}"
        assert _has_editor_guard(routes[key]), f"AI 额度入口没挂 require_editor: {path}"


def test_ai_routes_are_fully_accounted_for():
    """AI 路由的每个写方法要么在额度清单（挂闸），要么在簿记白名单（不挂）——不许有第三种。"""
    unaccounted = []
    for method, route in _iter_routes(ai_module.router):
        if method not in WRITE_METHODS:
            continue
        key = (method, route.path)
        if route.path in AI_EDITOR_ONLY_PATHS:
            assert _has_editor_guard(route), f"{key} 在额度清单里却没挂闸"
            continue
        if key in AI_BOOKKEEPING_WRITES:
            assert not _has_editor_guard(route), f"{key} 是簿记端点，不该挂闸"
            continue
        unaccounted.append(key)
    assert not unaccounted, f"这些 AI 写端点既不在额度清单也不在簿记白名单: {unaccounted}"


# ── 3) ASGI 行为 ───────────────────────────────────────────────────────────


def test_viewer_post_dashboard_returns_403():
    client = _client(UserRole.viewer)
    try:
        resp = client.post("/api/v1/dashboards", json={"title": "viewer 不许建"})
    finally:
        _cleanup_overrides()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


def test_viewer_ai_entrypoint_blocked_before_ai_config_check():
    """403 必须发生在"AI 未配置 503"之前——否则 viewer 看到的报错会误导成配置问题。"""
    client = _client(UserRole.viewer)
    try:
        resp = client.post("/api/v1/ai/query", json={"datasourceId": str(uuid.uuid4()), "question": "q"})
    finally:
        _cleanup_overrides()
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


def test_viewer_can_still_read_dashboard_list():
    from app.api.deps import get_dashboard_service

    fake_service = MagicMock()
    fake_service.list_dashboards = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.viewer)
    app.dependency_overrides[get_dashboard_service] = lambda: fake_service
    try:
        resp = TestClient(app).get("/api/v1/dashboards")
    finally:
        _cleanup_overrides()
    assert resp.status_code == 200, resp.text


def test_editor_can_create_dashboard():
    fake_service = MagicMock()
    fake_service.create = AsyncMock(return_value=SimpleNamespace(
        id=uuid.uuid4(), title="editor 可以建", description=None,
        user_id=uuid.uuid4(), created_at=None, updated_at=None, share_expires_at=None,
    ))
    from app.api.deps import get_dashboard_service

    app.dependency_overrides[get_current_user] = lambda: _user(UserRole.editor)
    app.dependency_overrides[get_dashboard_service] = lambda: fake_service
    try:
        resp = TestClient(app).post("/api/v1/dashboards", json={"title": "editor 可以建"})
    finally:
        _cleanup_overrides()
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["title"] == "editor 可以建"


# ── 4) 注册开关 ─────────────────────────────────────────────────────────────


def test_register_disabled_returns_403(monkeypatch):
    from app.api.v1 import auth as auth_module

    monkeypatch.setattr(auth_module.settings, "ALLOW_PUBLIC_REGISTER", False)
    resp = TestClient(app).post(
        "/api/v1/auth/register",
        json={"email": "new@example.invalid", "password": "12345678", "displayName": "n"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "REGISTER_DISABLED"


def test_register_enabled_still_works(monkeypatch):
    """默认 True 时必须保持既有行为（本地开发 / 既有测试不受影响）。"""
    from app.api.v1 import auth as auth_module
    from app.core.database import get_db

    fake_user = _user(UserRole.editor)
    fake_user.password_hash = "x"

    class _FakeService:
        def __init__(self, repo):
            pass

        async def register(self, email, password, display_name):
            return fake_user

    monkeypatch.setattr(auth_module.settings, "ALLOW_PUBLIC_REGISTER", True)
    monkeypatch.setattr(auth_module, "AuthService", _FakeService)
    fake_db = MagicMock()
    fake_db.commit = AsyncMock()
    app.dependency_overrides[get_db] = lambda: fake_db
    try:
        resp = TestClient(app).post(
            "/api/v1/auth/register",
            json={"email": "new@example.invalid", "password": "12345678", "displayName": "n"},
        )
    finally:
        _cleanup_overrides()
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["accessToken"]


# ── 5) 管理员停用账号 ───────────────────────────────────────────────────────


def _result(obj):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    r.scalar.return_value = obj
    return r


@pytest.mark.asyncio
async def test_admin_cannot_disable_self():
    admin = _user(UserRole.admin)
    with pytest.raises(HTTPException) as ei:
        await update_user_status(
            admin.id, UserStatusUpdateRequest(is_active=False),
            db=MagicMock(), current_user=admin,
        )
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "CANNOT_DISABLE_SELF"


@pytest.mark.asyncio
async def test_non_admin_cannot_change_status():
    viewer = _user(UserRole.viewer)
    with pytest.raises(HTTPException) as ei:
        await update_user_status(
            uuid.uuid4(), UserStatusUpdateRequest(is_active=False),
            db=MagicMock(), current_user=viewer,
        )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_can_disable_other_user():
    admin = _user(UserRole.admin)
    target = _user(UserRole.editor)
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(target))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    resp = await update_user_status(
        target.id, UserStatusUpdateRequest(is_active=False), db=db, current_user=admin,
    )
    assert target.is_active is False
    assert resp.data["isActive"] is False
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_cannot_disable_last_active_admin():
    admin = _user(UserRole.admin)
    other_admin = _user(UserRole.admin)
    db = MagicMock()
    # 第一次查目标用户，第二次查"还有几个启用中的 admin" → 0
    db.execute = AsyncMock(side_effect=[_result(other_admin), _result(0)])
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with pytest.raises(HTTPException) as ei:
        await update_user_status(
            other_admin.id, UserStatusUpdateRequest(is_active=False), db=db, current_user=admin,
        )
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "LAST_ADMIN"
    db.commit.assert_not_awaited()


def test_disabled_account_cannot_login(monkeypatch):
    """停用后连登录都要被拒：否则用户会"登录成功"然后每个请求都 403。"""
    from app.api.v1 import auth as auth_module
    from app.core.database import get_db

    disabled = _user(UserRole.editor, is_active=False)
    disabled.password_hash = "x"

    class _FakeService:
        def __init__(self, repo):
            pass

        async def authenticate(self, email, password):
            return disabled

    monkeypatch.setattr(auth_module, "AuthService", _FakeService)
    fake_db = MagicMock()
    fake_db.commit = AsyncMock()
    app.dependency_overrides[get_db] = lambda: fake_db
    try:
        resp = TestClient(app).post(
            "/api/v1/auth/login",
            json={"email": "disabled@example.invalid", "password": "12345678"},
        )
    finally:
        _cleanup_overrides()
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


def test_status_body_accepts_camel_case():
    body = UserStatusUpdateRequest.model_validate({"isActive": False})
    assert body.is_active is False

# ── 6) P4-A：指标 / 洞察 / 统计 / 回收站 + isActive 暴露 ─────────────────────


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/v1/metrics", {}),
        ("PATCH", f"/api/v1/metrics/{uuid.uuid4()}", {"name": "x"}),
        ("DELETE", f"/api/v1/metrics/{uuid.uuid4()}", None),
        ("POST", "/api/v1/insights/rules", {}),
        ("DELETE", f"/api/v1/insights/rules/{uuid.uuid4()}", None),
        ("POST", f"/api/v1/trash/canvas/{uuid.uuid4()}/restore", None),
    ],
)
def test_viewer_blocked_on_p4a_write_endpoints(method, path, body):
    """viewer 打指标/洞察/回收站的写端点 → 403（早于 body 校验，所以空 body 也应是 403）。"""
    client = _client(UserRole.viewer)
    try:
        resp = client.request(method, path, json=body) if body is not None else client.request(method, path)
    finally:
        _cleanup_overrides()
    assert resp.status_code == 403, f"{method} {path} 期望 403，实际 {resp.status_code}: {resp.text[:200]}"
    assert resp.json()["detail"]["code"] == "FORBIDDEN"


def test_editor_passes_metrics_guard():
    """editor 不该被 403 挡住：空 body 会走到 422（参数校验），说明已越过权限闸。"""
    client = _client(UserRole.editor)
    try:
        resp = client.post("/api/v1/metrics", json={})
    finally:
        _cleanup_overrides()
    assert resp.status_code != 403, f"editor 被误拦: {resp.text[:200]}"
    assert resp.status_code == 422


def test_read_only_statistics_stays_open_to_viewer():
    """statistics 的 6 个 POST 是无副作用的统计计算 → 不挂闸，viewer 应走到参数校验(422) 而不是 403。"""
    client = _client(UserRole.viewer)
    try:
        resp = client.post("/api/v1/statistics/describe", json={})
    finally:
        _cleanup_overrides()
    assert resp.status_code != 403, f"只读统计被误挂了写闸: {resp.text[:200]}"
    assert resp.status_code == 422


@pytest.mark.parametrize("role", [UserRole.viewer, UserRole.editor])
def test_trash_permanent_delete_is_admin_only(role):
    """彻底删除由更严格的 admin-only 内联闸守着（trash.py:148）：viewer/editor 都 403，且提示是管理员口径。"""
    client = _client(role)
    try:
        resp = client.delete(f"/api/v1/trash/canvas/{uuid.uuid4()}")
    finally:
        _cleanup_overrides()
    assert resp.status_code == 403, resp.text[:200]
    assert "管理员" in resp.json()["detail"]["message"]


def test_user_response_exposes_is_active():
    from app.schemas import UserResponse

    payload = UserResponse.model_validate(_user(UserRole.editor, is_active=False)).model_dump(
        mode="json", by_alias=True
    )
    assert payload["isActive"] is False
    # 既有字段仍在（没有因为新增字段而改变契约）
    assert payload["email"] == "probe@example.invalid"
    assert payload["role"] == "editor"


def test_user_response_is_active_defaults_true():
    """老调用方不传 is_active / 对象上没有该属性时，默认 True——新字段不能把既有构造炸掉。"""
    from app.schemas import UserResponse

    class _Lite:
        id = uuid.uuid4()
        email = "lite@example.invalid"
        display_name = "lite"
        avatar_url = None
        role = UserRole.editor
        created_at = datetime.now(timezone.utc)

    payload = UserResponse.model_validate(_Lite()).model_dump(mode="json", by_alias=True)
    assert payload["isActive"] is True


@pytest.mark.asyncio
async def test_admin_user_list_exposes_is_active():
    """管理界面靠 GET /permissions/users 看谁被停用 → 列表项必须带 isActive。"""
    admin = _user(UserRole.admin)
    disabled = _user(UserRole.editor, is_active=False)

    count_res = MagicMock()
    count_res.scalar.return_value = 1
    users_res = MagicMock()
    users_res.scalars.return_value.all.return_value = [disabled]
    empty_res = MagicMock()
    empty_res.all.return_value = []

    db = MagicMock()
    db.execute = AsyncMock(side_effect=[count_res, users_res, empty_res, empty_res, empty_res])

    resp = await list_users(1, 20, None, None, db, admin)
    item = resp.data["items"][0]
    assert item["isActive"] is False
    assert item["email"] == "probe@example.invalid"
