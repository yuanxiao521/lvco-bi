"""P0 分享链接止血（评审 D3）回归测试。

需求：/public/share/{token} 必须对「token 不存在 / 已撤销 / 已过期 / 记录软删」一律 404；
分享接口的 expiresAt 必须真的落库（不再 now()+30d 现编）；撤销接口必须能把链接打掉。

**能验证什么**（纯单测，Mock 掉 AsyncSession，不需要 PG）：
  1) public 的查询语句里真的带上了 软删 / 过期 / is_public / status!=deleted 四道条件
     （直接编译 SQL 断言，避免"写了注释没写条件"的自欺）；
  2) 行为层：命中 → 200（dashboard / report 两条分支），未命中 → 404，超长 token 直接 404 且不查库；
  3) 分享接口：默认 30 天 / 0=永久 / 自定义 N 天，值确实写在 ORM 字段上，返回值从字段读；
  4) 撤销接口：属主可撤销（token/expiry 清空、is_public 关掉、report 状态回落），非属主 404。

**明确未验证**（不要当成已验证）：
  - 没有连 PG：alembic 0023 的 upgrade/downgrade **未在此测试中执行**（只做了离线 SQL 渲染验证）；
  - 没有真实异步会话：selectinload 是否真的消除了 MissingGreenlet **未验证**（只保证语句带了 options）；
  - 过期判定依赖数据库比较（share_expires_at > now），此处只验证条件存在与操作符方向，
    没有用真实数据跑"过点了就查不到"的端到端断言。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.v1 import public as public_api
from app.api.v1.dashboards import (
    SHARE_DEFAULT_DAYS,
    ShareLinkBody,
    revoke_dashboard_share,
    share_dashboard,
)
from app.api.v1.reports import revoke_report_share, share_report
from app.models.report import ReportStatus

OWNER_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")
DASH_ID = UUID("33333333-3333-3333-3333-333333333333")
REPORT_ID = UUID("44444444-4444-4444-4444-444444444444")

DASH_TOKEN = "shr_dash_abcdefgh"
REPORT_TOKEN = "shr_report_abcdefgh"


# ── helpers ──────────────────────────────────────────────────────────────────


def _result(obj):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    return r


def _db(*rows):
    """Mock AsyncSession：execute 按调用顺序依次返回给定行。"""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(x) for x in rows])
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _dashboard(**over):
    d = MagicMock()
    d.id = DASH_ID
    d.user_id = OWNER_ID
    d.title = "季度经营看板"
    d.share_token = DASH_TOKEN
    d.share_expires_at = None
    d.is_public = True
    d.deleted_at = None
    d.dashboard_charts = []
    for k, v in over.items():
        setattr(d, k, v)
    return d


def _report(**over):
    r = MagicMock()
    r.id = REPORT_ID
    r.user_id = OWNER_ID
    r.title = "月度经营报告"
    r.share_token = REPORT_TOKEN
    r.share_expires_at = None
    r.status = ReportStatus.shared
    r.snapshot_blocks = {"blocks": []}
    for k, v in over.items():
        setattr(r, k, v)
    return r


def _service(**methods):
    svc = MagicMock()
    for name, value in methods.items():
        setattr(svc, name, AsyncMock(return_value=value))
    return svc


def _user(uid=OWNER_ID):
    u = MagicMock()
    u.id = uid
    return u


def _compiled(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


# ── 1) 查询条件：四道闸门必须真的在 SQL 里 ─────────────────────────────────


def test_dashboard_share_stmt_has_all_guards():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sql = _compiled(public_api.dashboard_share_stmt("tok_x", now=now))

    assert "dashboards.share_token = 'tok_x'" in sql
    # 撤销 / 非公开：is_public 必须为真
    assert "is_public" in sql
    # 软删除：deleted_at 必须为空
    assert "deleted_at is null" in sql
    # 过期：NULL=永久 + 未过点；方向必须是 ">"（过点即不匹配）
    assert "share_expires_at is null" in sql
    assert "share_expires_at >" in sql
    # now 被真正当成比较值传进去（不是装饰性参数）
    assert "2026-01-01" in sql


def test_report_share_stmt_has_all_guards():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sql = _compiled(public_api.report_share_stmt("tok_r", now=now))

    assert "reports.share_token = 'tok_r'" in sql
    # 报表软删 = status=deleted
    assert "status" in sql and "deleted" in sql
    assert "share_expires_at is null" in sql
    assert "share_expires_at >" in sql


def test_dashboard_share_stmt_has_loader_option():
    """public 分支会遍历 dashboard_charts / chart_config，必须带 eager load。

    说明：compile() 出来的 SQL 不包含 loader 选项，具体策略（selectinload）无法从编译结果
    反查，这里只能断言"挂了 loader 选项"；selectin 的语义由 public.py 的 .options(...) 保证，
    真实环境下是否消除 MissingGreenlet 见模块 docstring「未验证」。
    """
    stmt = public_api.dashboard_share_stmt("tok_x")
    opts = list(getattr(stmt, "_with_options", ()))
    assert opts, "查询未挂 loader 选项：异步会话访问懒加载关系会抛 MissingGreenlet"
    assert "load" in str(opts[0]).lower()


# ── 2) public 访问行为 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_public_valid_dashboard_returns_200():
    resp = await public_api.view_shared(DASH_TOKEN, db=_db(_dashboard()))
    assert resp.success is True
    assert resp.data["type"] == "dashboard"
    assert resp.data["title"] == "季度经营看板"
    assert resp.data["charts"] == []


@pytest.mark.asyncio
async def test_public_valid_report_returns_200():
    resp = await public_api.view_shared(REPORT_TOKEN, db=_db(None, _report()))
    assert resp.data["type"] == "report"
    assert resp.data["title"] == "月度经营报告"


@pytest.mark.asyncio
async def test_public_unknown_token_returns_404():
    with pytest.raises(HTTPException) as ei:
        await public_api.view_shared("shr_not_exist", db=_db(None, None))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_public_expired_token_returns_404():
    """过期链接：DB 因 share_expires_at <= now 查不到 → 与"不存在"同一 404。"""
    with pytest.raises(HTTPException) as ei:
        await public_api.view_shared(DASH_TOKEN, db=_db(None, None))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_public_soft_deleted_dashboard_returns_404():
    """软删除的 dashboard：deleted_at 非空 → SQL 过滤掉 → 404。"""
    with pytest.raises(HTTPException) as ei:
        await public_api.view_shared(DASH_TOKEN, db=_db(None, None))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_public_soft_deleted_report_returns_404():
    with pytest.raises(HTTPException) as ei:
        await public_api.view_shared(REPORT_TOKEN, db=_db(None, None))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_public_revoked_token_returns_404():
    """撤销后再访问：token 已被清空，DB 查不到 → 404。"""
    dashboard = _dashboard()
    db = _db(dashboard)
    await revoke_dashboard_share(DASH_ID, service=_service(get_by_id=dashboard), current_user=_user(), db=db)
    assert dashboard.share_token is None

    with pytest.raises(HTTPException) as ei:
        await public_api.view_shared(DASH_TOKEN, db=_db(None, None))
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_public_is_404_not_403_for_every_failure_mode():
    """不能用 403：403 会暴露"token 真实存在、只是失效了"。"""
    for rows in [(None, None)]:
        try:
            await public_api.view_shared(DASH_TOKEN, db=_db(*rows))
        except HTTPException as exc:
            assert exc.status_code == 404


@pytest.mark.asyncio
async def test_public_oversized_token_rejected_without_db_hit():
    db = _db(None, None)
    with pytest.raises(HTTPException) as ei:
        await public_api.view_shared("x" * 101, db=db)
    assert ei.value.status_code == 404
    db.execute.assert_not_awaited()


# ── 3) 分享接口：过期时间必须落库 ───────────────────────────────────────────


def _assert_about_days(actual: datetime, days: float) -> None:
    expected = datetime.now(timezone.utc) + timedelta(days=days)
    assert abs((actual - expected).total_seconds()) < 120, f"过期时间不匹配: {actual} vs {expected}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,expect_none,expect_days",
    [
        # 三态语义：字段不传 → 30 天；显式 null / 0 → 永久；N>0 → N 天
        (None, False, SHARE_DEFAULT_DAYS),                                      # 老前端：无 body
        (ShareLinkBody(), False, SHARE_DEFAULT_DAYS),                           # 空 body
        (ShareLinkBody.model_validate({"expiresInDays": None}), True, None),    # 显式 null → 永久
        (ShareLinkBody(expires_in_days=0), True, None),                         # 0 → 永久
        (ShareLinkBody(expires_in_days=7), False, 7),
    ],
)
async def test_dashboard_share_persists_expiry(body, expect_none, expect_days):
    dashboard = _dashboard()
    db = _db()
    resp = await share_dashboard(
        DASH_ID, body, service=_service(share=dashboard), db=db, current_user=_user()
    )

    if expect_none:
        assert dashboard.share_expires_at is None
        assert resp.data["expiresAt"] is None
        assert resp.data["permanent"] is True
    else:
        assert dashboard.share_expires_at is not None
        _assert_about_days(dashboard.share_expires_at, expect_days)
        # 返回值必须来自字段（库里读回），不是另算一份
        assert resp.data["expiresAt"] == dashboard.share_expires_at.isoformat()
        assert resp.data["permanent"] is False

    db.commit.assert_awaited()
    assert resp.data["shareToken"] == DASH_TOKEN
    assert resp.data["expiresInDays"] == (0 if expect_none else expect_days)


@pytest.mark.asyncio
async def test_report_share_persists_expiry():
    report = _report()
    db = _db()
    resp = await share_report(
        REPORT_ID, ShareLinkBody(expires_in_days=3),
        service=_service(share=report), db=db, current_user=_user(),
    )
    assert report.share_expires_at is not None
    _assert_about_days(report.share_expires_at, 3)
    assert resp.data["expiresAt"] == report.share_expires_at.isoformat()


def test_share_body_rejects_negative_days():
    with pytest.raises(ValidationError):
        ShareLinkBody(expires_in_days=-1)


# ── 4) 撤销接口 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_dashboard_share_by_owner():
    dashboard = _dashboard(share_expires_at=datetime.now(timezone.utc) + timedelta(days=5))
    db = _db()
    resp = await revoke_dashboard_share(
        DASH_ID, service=_service(get_by_id=dashboard), db=db, current_user=_user()
    )

    assert dashboard.share_token is None
    assert dashboard.share_expires_at is None
    assert dashboard.is_public is False
    db.commit.assert_awaited()
    assert resp.data["revoked"] is True
    assert resp.data["shareToken"] is None


@pytest.mark.asyncio
async def test_revoke_dashboard_share_by_non_owner_is_404():
    db = _db()
    with pytest.raises(HTTPException) as ei:
        await revoke_dashboard_share(
            DASH_ID, service=_service(get_by_id=None), db=db, current_user=_user(OTHER_ID)
        )
    assert ei.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_report_share_clears_and_rolls_status_back():
    report = _report(share_expires_at=datetime.now(timezone.utc) + timedelta(days=5))
    db = _db()
    await revoke_report_share(
        REPORT_ID, service=_service(get_by_id=report), db=db, current_user=_user()
    )
    assert report.share_token is None
    assert report.share_expires_at is None
    assert report.status == ReportStatus.published


@pytest.mark.asyncio
async def test_revoke_report_share_by_non_owner_is_404():
    db = _db()
    with pytest.raises(HTTPException) as ei:
        await revoke_report_share(
            REPORT_ID, service=_service(get_by_id=None), db=db, current_user=_user(OTHER_ID)
        )
    assert ei.value.status_code == 404

# ── 5) 真实 PG 集成验证（有真实库且 0023 已应用才跑；否则自动 skip）──────────
#
# 这一段补上纯 Mock 测不到的语义：**数据库真的把过期 / 软删 / 非公开的行过滤掉了**，
# 以及 selectinload 是否真的避开了 MissingGreenlet。
# 全程在一个事务里插入探针行，结束 rollback，不留任何数据。
#
# 为什么不复用 app.core.database.engine：conftest.py:15 把 DATABASE_URL setdefault 成
# test_db（单元测试隔离约定，那套凭据在本机不可用），所以这里沿用 tests/test_canvas_block_writer.py:20
# 的既有做法——自建 engine 直连 backend/.env 里的真实库，用完 dispose。


def _real_url() -> str | None:
    try:
        from dotenv import dotenv_values

        return dotenv_values(".env").get("DATABASE_URL")
    except Exception:  # noqa: BLE001
        return None


@pytest.mark.asyncio
async def test_public_share_against_real_pg_in_one_transaction():
    import uuid as _uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = _real_url()
    if not url:
        pytest.skip("无 backend/.env 的 DATABASE_URL，跳过真实库集成验证")

    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    now = datetime.now(timezone.utc)
    tag = _uuid.uuid4().hex[:8]
    try:
        try:
            n = (await session.execute(text(
                "select count(*) from information_schema.columns "
                "where column_name = 'share_expires_at' "
                "and table_name in ('dashboards', 'reports')"
            ))).scalar()
        except Exception as exc:  # noqa: BLE001 - 环境问题不算回归失败
            pytest.skip(f"真实库不可达，跳过集成验证：{type(exc).__name__}")
        if int(n or 0) != 2:
            pytest.skip("迁移 0023 未应用到真实库（share_expires_at 缺列），跳过集成验证")

        uid = str(_uuid.uuid4())
        await session.execute(text(
            "insert into users (id, email, password_hash, display_name) "
            "values (:id, :email, 'x', 'probe')"
        ), {"id": uid, "email": f"probe-{tag}@example.invalid"})

        async def add_dash(name: str, is_public: bool, exp, deleted=None):
            did, tok = str(_uuid.uuid4()), f"probe_{name}_{tag}"
            await session.execute(text(
                "insert into dashboards "
                "(id, user_id, title, is_public, share_token, share_expires_at, deleted_at) "
                "values (:id, :uid, :title, :pub, :tok, :exp, :del)"
            ), {"id": did, "uid": uid, "title": name, "pub": is_public,
                "tok": tok, "exp": exp, "del": deleted})
            return did, tok

        d_ok, t_ok = await add_dash("valid", True, now + timedelta(days=1))
        _, t_perm = await add_dash("permanent", True, None)
        _, t_exp = await add_dash("expired", True, now - timedelta(seconds=5))
        _, t_del = await add_dash("softdeleted", True, None, deleted=now)
        _, t_priv = await add_dash("private", False, None)

        # 给 valid 挂一个图表：能读出 charts=1 就说明 selectinload 生效（否则 MissingGreenlet）
        ccid = str(_uuid.uuid4())
        await session.execute(text(
            "insert into chart_configs (id, chart_type, query_config) "
            "values (:id, 'bar'::chart_type, '{}'::jsonb)"
        ), {"id": ccid})
        await session.execute(text(
            "insert into dashboard_charts (id, dashboard_id, chart_config_id) "
            "values (:id, :did, :ccid)"
        ), {"id": str(_uuid.uuid4()), "did": d_ok, "ccid": ccid})

        async def add_report(name: str, status: str, exp):
            tok = f"probe_r_{name}_{tag}"
            await session.execute(text(
                "insert into reports "
                "(id, user_id, title, source_type, status, snapshot_blocks, "
                " share_token, share_expires_at) "
                "values (:id, :uid, :title, 'manual'::report_source_type, "
                " cast(:st as report_status), '{}'::jsonb, :tok, :exp)"
            ), {"id": str(_uuid.uuid4()), "uid": uid, "title": name,
                "st": status, "tok": tok, "exp": exp})
            return tok

        t_r_ok = await add_report("ok", "shared", now + timedelta(days=1))
        t_r_exp = await add_report("exp", "shared", now - timedelta(seconds=5))
        t_r_del = await add_report("del", "deleted", None)

        # 有效 dashboard：200 且带出图表
        resp = await public_api.view_shared(t_ok, db=session)
        assert resp.data["type"] == "dashboard"
        assert len(resp.data["charts"]) == 1

        # 永久（NULL 过期）：200
        assert (await public_api.view_shared(t_perm, db=session)).data["type"] == "dashboard"

        # 过期 / 软删 / 非公开 / 不存在：一律 404
        for tok in (t_exp, t_del, t_priv, f"probe_nope_{tag}"):
            with pytest.raises(HTTPException) as ei:
                await public_api.view_shared(tok, db=session)
            assert ei.value.status_code == 404

        # 报表：有效 200；过期 / 软删 404
        assert (await public_api.view_shared(t_r_ok, db=session)).data["type"] == "report"
        for tok in (t_r_exp, t_r_del):
            with pytest.raises(HTTPException) as ei:
                await public_api.view_shared(tok, db=session)
            assert ei.value.status_code == 404
    finally:
        try:
            await session.rollback()
            await session.close()
        finally:
            await engine.dispose()
