"""DashboardRepository 单元测试。

使用 Mock AsyncSession，验证 Repository 封装的 SQLAlchemy 调用逻辑。
注意：本测试不构造真实的 ORM 对象（避免触发 mapper configure），
而是用 Mock 验证 Repository 与 Session 的交互逻辑。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.repositories.dashboard_repository import (
    SQLAlchemyDashboardChartRepository,
    SQLAlchemyDashboardRepository,
)


# ── 常量 ─────────────────────────────────────────────────────────────────────

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
DASHBOARD_ID = UUID("22222222-2222-2222-2222-222222222222")
CHART_ID_1 = UUID("33333333-3333-3333-3333-333333333333")
CHART_CFG_ID_1 = UUID("44444444-4444-4444-4444-444444444444")


# ── mock helper ──────────────────────────────────────────────────────────────


def _make_result(rows: list | None = None, scalar: object = None) -> MagicMock:
    """构造 SQLAlchemy execute() 返回的 Result 对象。"""
    result = MagicMock()
    if rows is not None:
        scalars = MagicMock()
        scalars.all.return_value = rows
        result.scalars.return_value = scalars
    result.scalar.return_value = scalar
    result.scalar_one_or_none.return_value = scalar
    return result


def _make_dashboard_mock() -> MagicMock:
    """构造 Dashboard Mock（避免触发 SQLAlchemy mapper configure）。"""
    d = MagicMock()
    d.id = DASHBOARD_ID
    d.title = "测试仪表板"
    d.description = "测试"
    d.layout = []
    d.refresh_interval = 300
    d.deleted_at = None
    d.share_token = None
    d.is_public = False
    d.dashboard_charts = []
    return d


# ── SQLAlchemyDashboardRepository ────────────────────────────────────────────


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock AsyncSession：异步方法用 AsyncMock，同步方法用 MagicMock。"""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.delete = MagicMock()
    return db


@pytest.fixture
def dashboard_repo(mock_db: AsyncMock) -> SQLAlchemyDashboardRepository:
    return SQLAlchemyDashboardRepository(mock_db)


class TestDashboardRepositoryCreate:
    """create 方法测试。

    验证 Repository 正确调用 db.add / db.flush / db.refresh。
    不构造真实 ORM 对象，使用 Mock 替代。
    """

    @pytest.mark.asyncio
    async def test_create_calls_db_methods(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试 create 调用 add + flush + refresh。"""
        with patch("app.repositories.dashboard_repository.Dashboard") as MockDashboard:
            mock_instance = MagicMock()
            MockDashboard.return_value = mock_instance

            await dashboard_repo.create(
                user_id=USER_ID, title="新仪表板", description="描述"
            )

            MockDashboard.assert_called_once()
            call_kwargs = MockDashboard.call_args.kwargs
            assert call_kwargs["user_id"] == USER_ID
            assert call_kwargs["title"] == "新仪表板"
            assert call_kwargs["description"] == "描述"
            assert call_kwargs["layout"] == []
            mock_db.add.assert_called_once_with(mock_instance)
            mock_db.flush.assert_called_once()
            mock_db.refresh.assert_called_once_with(mock_instance)

    @pytest.mark.asyncio
    async def test_create_without_description(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试不传 description 时 description 为 None。"""
        with patch("app.repositories.dashboard_repository.Dashboard") as MockDashboard:
            MockDashboard.return_value = MagicMock()

            await dashboard_repo.create(user_id=USER_ID, title="无描述")

            call_kwargs = MockDashboard.call_args.kwargs
            assert call_kwargs["description"] is None


class TestDashboardRepositoryList:
    """list_dashboards 方法测试。"""

    @pytest.mark.asyncio
    async def test_list_dashboards_returns_items_and_total(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试列表返回 (items, total)。"""
        dashboards = [_make_dashboard_mock(), _make_dashboard_mock()]
        mock_db.execute.side_effect = [
            _make_result(scalar=2),  # count query
            _make_result(rows=dashboards),  # list query
        ]

        items, total = await dashboard_repo.list_dashboards(
            user_id=USER_ID, page=1, page_size=20, search=None
        )

        assert total == 2
        assert items == dashboards
        assert mock_db.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_list_dashboards_with_search(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试带搜索条件：执行 2 次查询（count + list）。"""
        mock_db.execute.side_effect = [
            _make_result(scalar=0),
            _make_result(rows=[]),
        ]

        items, total = await dashboard_repo.list_dashboards(
            user_id=USER_ID, page=1, page_size=20, search="销售"
        )

        assert total == 0
        assert items == []
        assert mock_db.execute.call_count == 2


class TestDashboardRepositoryGetById:
    """get_by_id 方法测试。"""

    @pytest.mark.asyncio
    async def test_get_by_id_found(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试找到仪表板。"""
        dashboard = _make_dashboard_mock()
        mock_db.execute.return_value = _make_result(scalar=dashboard)

        result = await dashboard_repo.get_by_id(DASHBOARD_ID, USER_ID)

        assert result is dashboard

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试未找到仪表板。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await dashboard_repo.get_by_id(DASHBOARD_ID, USER_ID)

        assert result is None


class TestDashboardRepositoryUpdateLayout:
    """update_layout 方法测试。"""

    @pytest.mark.asyncio
    async def test_update_layout_success(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试成功更新布局。"""
        dashboard = _make_dashboard_mock()
        dashboard.layout = []
        mock_db.execute.return_value = _make_result(scalar=dashboard)

        layout = [{"i": "chart-1", "x": 0, "y": 0}]
        result = await dashboard_repo.update_layout(DASHBOARD_ID, USER_ID, layout)

        assert result is dashboard
        assert dashboard.layout == layout
        mock_db.flush.assert_called_once()
        mock_db.refresh.assert_called_once_with(dashboard)

    @pytest.mark.asyncio
    async def test_update_layout_not_found(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试仪表板不存在时返回 None。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await dashboard_repo.update_layout(DASHBOARD_ID, USER_ID, [])

        assert result is None
        mock_db.flush.assert_not_called()


class TestDashboardRepositoryDelete:
    """delete 方法测试。"""

    @pytest.mark.asyncio
    async def test_delete_soft_delete(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试软删除：设置 deleted_at 字段。"""
        dashboard = _make_dashboard_mock()
        dashboard.deleted_at = None
        mock_db.execute.return_value = _make_result(scalar=dashboard)

        result = await dashboard_repo.delete(DASHBOARD_ID, USER_ID)

        assert result is True
        assert dashboard.deleted_at is not None
        mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_not_found(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试未找到时返回 False。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await dashboard_repo.delete(DASHBOARD_ID, USER_ID)

        assert result is False


class TestDashboardRepositoryShare:
    """share 方法测试。"""

    @pytest.mark.asyncio
    async def test_share_generates_token(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试分享时生成 share_token。"""
        dashboard = _make_dashboard_mock()
        dashboard.share_token = None
        dashboard.is_public = False
        mock_db.execute.return_value = _make_result(scalar=dashboard)

        result = await dashboard_repo.share(DASHBOARD_ID, USER_ID)

        assert result is dashboard
        assert result.share_token is not None
        assert result.share_token.startswith("shr_")
        assert result.is_public is True

    @pytest.mark.asyncio
    async def test_share_preserves_existing_token(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试已有 token 时保留不变。"""
        existing_token = "shr_existing-token"
        dashboard = _make_dashboard_mock()
        dashboard.share_token = existing_token
        dashboard.is_public = False
        mock_db.execute.return_value = _make_result(scalar=dashboard)

        result = await dashboard_repo.share(DASHBOARD_ID, USER_ID)

        assert result.share_token == existing_token

    @pytest.mark.asyncio
    async def test_share_not_found(
        self,
        dashboard_repo: SQLAlchemyDashboardRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试未找到时返回 None。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await dashboard_repo.share(DASHBOARD_ID, USER_ID)

        assert result is None


# ── SQLAlchemyDashboardChartRepository ────────────────────────────────────────


@pytest.fixture
def chart_repo(mock_db: AsyncMock) -> SQLAlchemyDashboardChartRepository:
    return SQLAlchemyDashboardChartRepository(mock_db)


def _make_dashboard_chart_mock() -> MagicMock:
    """构造 DashboardChart Mock。"""
    dc = MagicMock()
    dc.id = CHART_ID_1
    dc.dashboard_id = DASHBOARD_ID
    dc.chart_config_id = CHART_CFG_ID_1
    dc.title = "图表 1"
    dc.position = None
    return dc


class TestDashboardChartRepositoryAdd:
    """add_chart 方法测试。"""

    @pytest.mark.asyncio
    async def test_add_chart_success(
        self,
        chart_repo: SQLAlchemyDashboardChartRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试成功添加图表。"""
        # 第一次 execute 校验 chart_config 存在
        mock_db.execute.return_value = _make_result(scalar=MagicMock())

        with patch(
            "app.repositories.dashboard_repository.DashboardChart"
        ) as MockDC:
            mock_dc = MagicMock()
            MockDC.return_value = mock_dc

            result = await chart_repo.add_chart(
                dashboard_id=DASHBOARD_ID,
                chart_config_id=CHART_CFG_ID_1,
                title="图表 1",
                position={"x": 0, "y": 0},
            )

            assert result is mock_dc
            MockDC.assert_called_once()
            mock_db.add.assert_called_once_with(mock_dc)
            mock_db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_chart_config_not_found(
        self,
        chart_repo: SQLAlchemyDashboardChartRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试 ChartConfig 不存在时返回 None。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await chart_repo.add_chart(
            dashboard_id=DASHBOARD_ID,
            chart_config_id=CHART_CFG_ID_1,
        )

        assert result is None
        mock_db.add.assert_not_called()


class TestDashboardChartRepositoryRemove:
    """remove_chart 方法测试。"""

    @pytest.mark.asyncio
    async def test_remove_chart_success(
        self,
        chart_repo: SQLAlchemyDashboardChartRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试成功移除图表。"""
        dc = _make_dashboard_chart_mock()
        mock_db.execute.return_value = _make_result(scalar=dc)

        result = await chart_repo.remove_chart(
            dashboard_id=DASHBOARD_ID,
            chart_id=CHART_ID_1,
            user_id=USER_ID,
        )

        assert result is True
        mock_db.delete.assert_called_once_with(dc)

    @pytest.mark.asyncio
    async def test_remove_chart_not_found(
        self,
        chart_repo: SQLAlchemyDashboardChartRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试图表不存在时返回 False。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await chart_repo.remove_chart(
            dashboard_id=DASHBOARD_ID,
            chart_id=CHART_ID_1,
            user_id=USER_ID,
        )

        assert result is False
        mock_db.delete.assert_not_called()


class TestDashboardChartRepositoryGetConfig:
    """get_chart_config 方法测试。"""

    @pytest.mark.asyncio
    async def test_get_chart_config_found(
        self,
        chart_repo: SQLAlchemyDashboardChartRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试成功查询 ChartConfig。"""
        chart_config = MagicMock(id=CHART_CFG_ID_1)
        mock_db.execute.return_value = _make_result(scalar=chart_config)

        result = await chart_repo.get_chart_config(CHART_CFG_ID_1)

        assert result is chart_config

    @pytest.mark.asyncio
    async def test_get_chart_config_not_found(
        self,
        chart_repo: SQLAlchemyDashboardChartRepository,
        mock_db: AsyncMock,
    ) -> None:
        """测试未找到时返回 None。"""
        mock_db.execute.return_value = _make_result(scalar=None)

        result = await chart_repo.get_chart_config(CHART_CFG_ID_1)

        assert result is None