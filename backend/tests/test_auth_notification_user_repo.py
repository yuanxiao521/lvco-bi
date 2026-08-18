"""新增 Repository / Service 单元测试。

覆盖：
- UserRepository
- NotificationRepository
- AuthService
- NotificationService

使用 Mock AsyncSession + Mock Repository，完全隔离数据库。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.repositories.notification_repository import SQLAlchemyNotificationRepository
from app.repositories.user_repository import SQLAlchemyUserRepository
from app.services.auth_service import AuthService
from app.services.notification_service import NotificationService


# ── 常量 ─────────────────────────────────────────────────────────────────────

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
NOTIF_ID = UUID("22222222-2222-2222-2222-222222222222")


def _make_result(rows: list | None = None, scalar=None, rowcount: int = 0) -> MagicMock:
    """构造 SQLAlchemy execute() 返回 Result。"""
    result = MagicMock()
    if rows is not None:
        scalars = MagicMock()
        scalars.all.return_value = rows
        result.scalars.return_value = scalars
    result.scalar.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    result.rowcount = rowcount
    return result


# ═══════════════════════════════════════════════════════════════════════════
# UserRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_db() -> MagicMock:
    """AsyncSession 的 mock：
    - execute/flush/refresh/commit：异步（AsyncMock）
    - add/delete/close：同步（MagicMock）
    """
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    # add/delete 是同步方法，不应该是 AsyncMock
    db.add = MagicMock()
    db.delete = MagicMock()
    return db


@pytest.fixture
def user_repo(mock_db: AsyncMock) -> SQLAlchemyUserRepository:
    return SQLAlchemyUserRepository(mock_db)


class TestUserRepositoryGet:
    @pytest.mark.asyncio
    async def test_get_by_id_found(self, user_repo, mock_db):
        u = MagicMock()
        mock_db.execute.return_value = _make_result(scalar=u)
        result = await user_repo.get_by_id(USER_ID)
        assert result is u

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, user_repo, mock_db):
        mock_db.execute.return_value = _make_result(scalar=None)
        assert await user_repo.get_by_id(USER_ID) is None

    @pytest.mark.asyncio
    async def test_get_by_email(self, user_repo, mock_db):
        u = MagicMock()
        mock_db.execute.return_value = _make_result(scalar=u)
        result = await user_repo.get_by_email("a@b.com")
        assert result is u


class TestUserRepositoryCreate:
    @pytest.mark.asyncio
    async def test_create_calls_db_methods(self, user_repo, mock_db):
        with patch("app.repositories.user_repository.User") as MockU:
            mock_u = MagicMock()
            MockU.return_value = mock_u
            result = await user_repo.create(
                email="a@b.com",
                password_hash="hashed",
                display_name="Tom",
                role="admin",
            )
            MockU.assert_called_once()
            kw = MockU.call_args.kwargs
            assert kw["email"] == "a@b.com"
            assert kw["password_hash"] == "hashed"
            assert kw["display_name"] == "Tom"
            assert kw["role"] == "admin"
            mock_db.add.assert_called_once_with(mock_u)
            mock_db.flush.assert_called_once()
            mock_db.refresh.assert_called_once_with(mock_u)
            assert result is mock_u


class TestUserRepositoryUpdate:
    @pytest.mark.asyncio
    async def test_update_no_changes_returns_existing(self, user_repo, mock_db):
        u = MagicMock()
        mock_db.execute.return_value = _make_result(scalar=u)
        result = await user_repo.update(USER_ID)
        assert result is u

    @pytest.mark.asyncio
    async def test_update_partial_fields(self, user_repo, mock_db):
        u = MagicMock()
        mock_db.execute.return_value = _make_result(scalar=u)
        result = await user_repo.update(
            USER_ID, display_name="Alice", avatar_url="http://x"
        )
        assert result is u


class TestUserRepositoryDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self, user_repo, mock_db):
        u = MagicMock()
        mock_db.execute.return_value = _make_result(scalar=u)
        assert await user_repo.delete(USER_ID) is True
        mock_db.delete.assert_called_once_with(u)

    @pytest.mark.asyncio
    async def test_delete_not_found(self, user_repo, mock_db):
        mock_db.execute.return_value = _make_result(scalar=None)
        assert await user_repo.delete(USER_ID) is False


# ═══════════════════════════════════════════════════════════════════════════
# NotificationRepository
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def notif_repo(mock_db: AsyncMock) -> SQLAlchemyNotificationRepository:
    return SQLAlchemyNotificationRepository(mock_db)


class TestNotificationRepository:
    @pytest.mark.asyncio
    async def test_create_calls_db_methods(self, notif_repo, mock_db):
        with patch("app.repositories.notification_repository.Notification") as MockN:
            mock_n = MagicMock()
            MockN.return_value = mock_n
            await notif_repo.create(
                user_id=USER_ID,
                notif_type="info",
                title="Title",
                body="Body",
            )
            mock_db.add.assert_called_once_with(mock_n)
            mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_count_unread(self, notif_repo, mock_db):
        mock_db.execute.return_value = _make_result(scalar=3)
        assert await notif_repo.count_unread(USER_ID) == 3

    @pytest.mark.asyncio
    async def test_mark_read_success(self, notif_repo, mock_db):
        mock_db.execute.return_value = _make_result(rowcount=1)
        assert await notif_repo.mark_read(USER_ID, NOTIF_ID) is True

    @pytest.mark.asyncio
    async def test_mark_read_not_found(self, notif_repo, mock_db):
        mock_db.execute.return_value = _make_result(rowcount=0)
        assert await notif_repo.mark_read(USER_ID, NOTIF_ID) is False

    @pytest.mark.asyncio
    async def test_mark_all_read(self, notif_repo, mock_db):
        mock_db.execute.return_value = _make_result(rowcount=5)
        assert await notif_repo.mark_all_read(USER_ID) == 5


# ═══════════════════════════════════════════════════════════════════════════
# AuthService
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_user_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def auth_service(mock_user_repo: AsyncMock) -> AuthService:
    return AuthService(user_repo=mock_user_repo)


class TestAuthServiceAuthenticate:
    @pytest.mark.asyncio
    async def test_authenticate_user_not_found(self, auth_service, mock_user_repo):
        mock_user_repo.get_by_email.return_value = None
        assert await auth_service.authenticate("a@b.com", "pwd") is None

    @pytest.mark.asyncio
    async def test_authenticate_wrong_password(self, auth_service, mock_user_repo):
        user = MagicMock(password_hash="hashed")
        mock_user_repo.get_by_email.return_value = user
        with patch("app.services.auth_service.verify_password", return_value=False):
            assert await auth_service.authenticate("a@b.com", "wrong") is None

    @pytest.mark.asyncio
    async def test_authenticate_success(self, auth_service, mock_user_repo):
        user = MagicMock(password_hash="hashed")
        mock_user_repo.get_by_email.return_value = user
        with patch("app.services.auth_service.verify_password", return_value=True):
            result = await auth_service.authenticate("a@b.com", "pwd")
            assert result is user


class TestAuthServiceRegister:
    @pytest.mark.asyncio
    async def test_register_email_exists(self, auth_service, mock_user_repo):
        mock_user_repo.get_by_email.return_value = MagicMock()
        with pytest.raises(ValueError, match="已被注册"):
            await auth_service.register("a@b.com", "pwd", "Tom")

    @pytest.mark.asyncio
    async def test_register_success(self, auth_service, mock_user_repo):
        mock_user_repo.get_by_email.return_value = None
        new_user = MagicMock()
        mock_user_repo.create.return_value = new_user
        with patch("app.services.auth_service.hash_password", return_value="hashed"):
            result = await auth_service.register("a@b.com", "pwd", "Tom")
        mock_user_repo.create.assert_called_once_with(
            email="a@b.com", password_hash="hashed", display_name="Tom"
        )
        assert result is new_user


class TestAuthServiceTokens:
    def test_create_tokens(self, auth_service):
        user = MagicMock(id=USER_ID, email="a@b.com")
        with patch("app.services.auth_service.create_access_token", return_value="acc"):
            with patch("app.services.auth_service.create_refresh_token", return_value="ref"):
                tokens = auth_service.create_tokens(user)
        assert tokens["accessToken"] == "acc"
        assert tokens["refreshToken"] == "ref"
        assert tokens["tokenType"] == "bearer"

    def test_refresh_access_token_invalid(self, auth_service):
        with patch("app.services.auth_service.decode_token", return_value=None):
            assert auth_service.refresh_access_token("xxx") is None

    def test_refresh_access_token_wrong_type(self, auth_service):
        with patch(
            "app.services.auth_service.decode_token",
            return_value={"type": "access", "sub": str(USER_ID)},
        ):
            assert auth_service.refresh_access_token("xxx") is None

    def test_refresh_access_token_success(self, auth_service):
        with patch(
            "app.services.auth_service.decode_token",
            return_value={"type": "refresh", "sub": str(USER_ID)},
        ):
            with patch(
                "app.services.auth_service.create_access_token", return_value="new_acc"
            ):
                assert auth_service.refresh_access_token("xxx") == "new_acc"


# ═══════════════════════════════════════════════════════════════════════════
# NotificationService
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_notif_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def notif_service(mock_notif_repo: AsyncMock) -> NotificationService:
    return NotificationService(notif_repo=mock_notif_repo)


class TestNotificationServicePush:
    @pytest.mark.asyncio
    async def test_push_creates_and_publishes_sse(self, notif_service, mock_notif_repo):
        mock_notif = MagicMock()
        mock_notif.id = NOTIF_ID
        mock_notif.type = MagicMock()
        mock_notif.type.value = "system"
        mock_notif.title = "T"
        mock_notif.body = "B"
        mock_notif.link_url = None
        mock_notif.resource_type = None
        mock_notif.resource_id = None
        mock_notif.created_at = None
        mock_notif_repo.create.return_value = mock_notif

        with patch("app.services.notification_service.sse_manager") as mock_sse:
            mock_sse.publish = AsyncMock()
            result = await notif_service.push(
                user_id=USER_ID,
                type_="system",
                title="Title",
                body="Body",
            )

        mock_notif_repo.create.assert_called_once()
        mock_sse.publish.assert_called_once()
        assert result is mock_notif

    @pytest.mark.asyncio
    async def test_push_sse_failure_does_not_break(self, notif_service, mock_notif_repo):
        mock_notif = MagicMock()
        mock_notif.id = NOTIF_ID
        mock_notif.type = MagicMock()
        mock_notif.type.value = "system"
        mock_notif.title = "T"
        mock_notif.body = "B"
        mock_notif.link_url = None
        mock_notif.resource_type = None
        mock_notif.resource_id = None
        mock_notif.created_at = None
        mock_notif_repo.create.return_value = mock_notif

        with patch("app.services.notification_service.sse_manager") as mock_sse:
            mock_sse.publish = AsyncMock(side_effect=RuntimeError("conn lost"))
            result = await notif_service.push(
                user_id=USER_ID, type_="system", title="T", body="B"
            )
        assert result is mock_notif  # DB 写入仍成功


class TestNotificationServiceList:
    @pytest.mark.asyncio
    async def test_list_calls_repo(self, notif_service, mock_notif_repo):
        items = [MagicMock()]
        mock_notif_repo.list_for_user.return_value = (items, 1)
        result_items, total = await notif_service.list(USER_ID, page=1, page_size=20)
        assert result_items == items
        assert total == 1

    @pytest.mark.asyncio
    async def test_unread_count(self, notif_service, mock_notif_repo):
        mock_notif_repo.count_unread.return_value = 5
        assert await notif_service.unread_count(USER_ID) == 5

    @pytest.mark.asyncio
    async def test_mark_read(self, notif_service, mock_notif_repo):
        mock_notif_repo.mark_read.return_value = True
        assert await notif_service.mark_read(NOTIF_ID, USER_ID) is True

    @pytest.mark.asyncio
    async def test_mark_all_read(self, notif_service, mock_notif_repo):
        mock_notif_repo.mark_all_read.return_value = 3
        assert await notif_service.mark_all_read(USER_ID) == 3

    @pytest.mark.asyncio
    async def test_clear_all(self, notif_service, mock_notif_repo):
        mock_notif_repo.clear_all.return_value = 2
        assert await notif_service.clear_all(USER_ID) == 2
