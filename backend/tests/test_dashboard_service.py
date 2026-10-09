"""DashboardService 单元测试。

使用 Mock Repository，完全隔离数据库，验证业务逻辑。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.services.dashboard_service import DashboardService


# ── 测试 fixture ─────────────────────────────────────────────────────────────

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
DASHBOARD_ID = UUID("22222222-2222-2222-2222-222222222222")
CHART_ID_1 = UUID("33333333-3333-3333-3333-333333333333")
CHART_CFG_ID_1 = UUID("44444444-4444-4444-4444-444444444444")


def _make_dashboard_mock(
    dashboard_id: UUID = DASHBOARD_ID,
    title: str = "销售仪表板",
    description: str | None = "Q1 销售分析",
    layout: list | None = None,
    refresh_interval: int = 300,
    dashboard_charts: list | None = None,
) -> MagicMock:
    """构造 Dashboard Mock 对象。"""
    d = MagicMock()
    d.id = dashboard_id
    d.title = title
    d.description = description
    d.layout = layout if layout is not None else []
    d.refresh_interval = refresh_interval
    d.dashboard_charts = dashboard_charts if dashboard_charts is not None else []
    return d


def _make_dashboard_chart_mock(
    chart_id: UUID = CHART_ID_1,
    chart_config_id: UUID = CHART_CFG_ID_1,
    title: str = "图表 1",
    position: dict | None = None,
) -> MagicMock:
    """构造 DashboardChart Mock 对象。"""
    dc = MagicMock()
    dc.id = chart_id
    dc.chart_config_id = chart_config_id
    dc.title = title
    dc.position = position
    return dc


def _make_chart_config_mock(
    chart_type: str = "line",
    query_config: dict | None = None,
    render_config: dict | None = None,
) -> MagicMock:
    """构造 ChartConfig Mock 对象。"""
    cc = MagicMock()
    cc.chart_type = MagicMock()
    cc.chart_type.value = chart_type
    cc.query_config = query_config or {}
    cc.render_config = render_config
    return cc


@pytest.fixture
def mock_dashboard_repo() -> AsyncMock:
    """Mock DashboardRepository。"""
    return AsyncMock()


@pytest.fixture
def mock_dashboard_chart_repo() -> AsyncMock:
    """Mock DashboardChartRepository。"""
    return AsyncMock()


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock AsyncSession（用于 execute_chart_query 依赖）。"""
    return MagicMock()


@pytest.fixture
def mock_cache_repo() -> MagicMock:
    """注入的内存缓存 Mock（符合 CacheRepository 协议）。"""
    return MagicMock()

@pytest.fixture
def service(
    mock_dashboard_repo: AsyncMock,
    mock_dashboard_chart_repo: AsyncMock,
    mock_db: MagicMock,
    mock_cache_repo: MagicMock,
) -> DashboardService:
    """构造 DashboardService，注入 Mock Repository 与缓存仓库。"""
    return DashboardService(
        dashboard_repo=mock_dashboard_repo,
        dashboard_chart_repo=mock_dashboard_chart_repo,
        db=mock_db,
        cache_repo=mock_cache_repo,
    )


# ── create 方法 ──────────────────────────────────────────────────────────────


class TestDashboardServiceCreate:
    """create 方法测试。"""

    @pytest.mark.asyncio
    async def test_create_calls_repo(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试 create 调用 Repository。"""
        expected = _make_dashboard_mock(title="新仪表板")
        mock_dashboard_repo.create.return_value = expected

        result = await service.create(
            user_id=USER_ID,
            title="新仪表板",
            description="测试描述",
        )

        mock_dashboard_repo.create.assert_called_once_with(
            USER_ID, "新仪表板", "测试描述"
        )
        assert result is expected

    @pytest.mark.asyncio
    async def test_create_with_no_description(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试不传 description 时正常调用。"""
        expected = _make_dashboard_mock()
        mock_dashboard_repo.create.return_value = expected

        await service.create(user_id=USER_ID, title="无描述仪表板")

        mock_dashboard_repo.create.assert_called_once_with(
            USER_ID, "无描述仪表板", None
        )


# ── list_dashboards 方法 ─────────────────────────────────────────────────────


class TestDashboardServiceList:
    """list_dashboards 方法测试。"""

    @pytest.mark.asyncio
    async def test_list_dashboards_calls_repo(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试列表方法调用 Repository。"""
        items = [_make_dashboard_mock()]
        mock_dashboard_repo.list_dashboards.return_value = (items, 1)

        result_items, result_total = await service.list_dashboards(
            user_id=USER_ID, page=1, page_size=20, search="销售"
        )

        mock_dashboard_repo.list_dashboards.assert_called_once_with(
            USER_ID, 1, 20, "销售"
        )
        assert result_items == items
        assert result_total == 1

    @pytest.mark.asyncio
    async def test_list_dashboards_empty(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试空列表场景。"""
        mock_dashboard_repo.list_dashboards.return_value = ([], 0)

        items, total = await service.list_dashboards(
            user_id=USER_ID, page=1, page_size=20, search=None
        )

        assert items == []
        assert total == 0


# ── get_by_id 方法 ───────────────────────────────────────────────────────────


class TestDashboardServiceGetById:
    """get_by_id 方法测试。"""

    @pytest.mark.asyncio
    async def test_get_by_id_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试找到仪表板。"""
        expected = _make_dashboard_mock()
        mock_dashboard_repo.get_by_id.return_value = expected

        result = await service.get_by_id(DASHBOARD_ID, USER_ID)

        mock_dashboard_repo.get_by_id.assert_called_once_with(DASHBOARD_ID, USER_ID)
        assert result is expected

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试未找到仪表板。"""
        mock_dashboard_repo.get_by_id.return_value = None

        result = await service.get_by_id(DASHBOARD_ID, USER_ID)

        assert result is None


# ── add_chart / remove_chart 方法 ────────────────────────────────────────────


class TestDashboardServiceAddChart:
    """add_chart 方法测试。"""

    @pytest.mark.asyncio
    async def test_add_chart_dashboard_not_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试仪表板不存在时返回 None。"""
        mock_dashboard_repo.get_by_id.return_value = None

        result = await service.add_chart(
            dashboard_id=DASHBOARD_ID,
            user_id=USER_ID,
            chart_config_id=CHART_CFG_ID_1,
        )

        assert result is None
        mock_dashboard_chart_repo.add_chart.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_chart_success(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试成功添加图表。"""
        dashboard = _make_dashboard_mock()
        dc = _make_dashboard_chart_mock()
        mock_dashboard_repo.get_by_id.return_value = dashboard
        mock_dashboard_chart_repo.add_chart.return_value = dc

        result = await service.add_chart(
            dashboard_id=DASHBOARD_ID,
            user_id=USER_ID,
            chart_config_id=CHART_CFG_ID_1,
            title="新图表",
            position={"x": 0, "y": 0},
        )

        mock_dashboard_chart_repo.add_chart.assert_called_once_with(
            DASHBOARD_ID, CHART_CFG_ID_1, "新图表", {"x": 0, "y": 0}
        )
        assert result is dc


class TestDashboardServiceRemoveChart:
    """remove_chart 方法测试。"""

    @pytest.mark.asyncio
    async def test_remove_chart_dashboard_not_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试仪表板不存在时返回 False。"""
        mock_dashboard_repo.get_by_id.return_value = None

        result = await service.remove_chart(
            dashboard_id=DASHBOARD_ID,
            chart_id=CHART_ID_1,
            user_id=USER_ID,
        )

        assert result is False
        mock_dashboard_chart_repo.remove_chart.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_chart_success(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试成功移除图表。"""
        mock_dashboard_repo.get_by_id.return_value = _make_dashboard_mock()
        mock_dashboard_chart_repo.remove_chart.return_value = True

        result = await service.remove_chart(
            dashboard_id=DASHBOARD_ID,
            chart_id=CHART_ID_1,
            user_id=USER_ID,
        )

        mock_dashboard_chart_repo.remove_chart.assert_called_once_with(
            DASHBOARD_ID, CHART_ID_1, USER_ID
        )
        assert result is True


# ── delete / share / update_layout 方法 ──────────────────────────────────────


class TestDashboardServiceBasicOps:
    """基础操作方法测试。"""

    @pytest.mark.asyncio
    async def test_delete(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试 delete 直接调用 Repository。"""
        mock_dashboard_repo.delete.return_value = True

        result = await service.delete(DASHBOARD_ID, USER_ID)

        mock_dashboard_repo.delete.assert_called_once_with(DASHBOARD_ID, USER_ID)
        assert result is True

    @pytest.mark.asyncio
    async def test_share(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试 share 直接调用 Repository。"""
        expected = _make_dashboard_mock()
        mock_dashboard_repo.share.return_value = expected

        result = await service.share(DASHBOARD_ID, USER_ID)

        mock_dashboard_repo.share.assert_called_once_with(DASHBOARD_ID, USER_ID)
        assert result is expected

    @pytest.mark.asyncio
    async def test_update_layout(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试 update_layout 直接调用 Repository。"""
        expected = _make_dashboard_mock()
        layout = [{"i": "chart-1", "x": 0, "y": 0}]
        mock_dashboard_repo.update_layout.return_value = expected

        result = await service.update_layout(DASHBOARD_ID, USER_ID, layout)

        mock_dashboard_repo.update_layout.assert_called_once_with(
            DASHBOARD_ID, USER_ID, layout
        )
        assert result is expected


# ── get_dashboard_data 方法 ──────────────────────────────────────────────────


class TestDashboardServiceGetData:
    """get_dashboard_data 方法测试。"""

    @pytest.mark.asyncio
    async def test_get_data_dashboard_not_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试仪表板不存在时返回 None。"""
        mock_dashboard_repo.get_by_id.return_value = None

        result = await service.get_dashboard_data(DASHBOARD_ID, USER_ID)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_data_uses_cache(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
        mock_cache_repo: MagicMock,
    ) -> None:
        """测试 use_cache=True 时命中缓存直接返回。"""
        cached_payload = {
            "dashboard_id": str(DASHBOARD_ID),
            "layout": [],
            "charts": [{"chart_id": "cached", "title": "Cached"}],
        }
        dashboard = _make_dashboard_mock()
        mock_dashboard_repo.get_by_id.return_value = dashboard

        mock_cache_repo.get.return_value = json.dumps(cached_payload)

        result = await service.get_dashboard_data(
            DASHBOARD_ID, USER_ID, use_cache=True
        )

        assert result == cached_payload
        mock_dashboard_chart_repo.get_chart_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_data_no_datasource_config(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试图表配置未设置数据源时返回 error。"""
        dc = _make_dashboard_chart_mock()
        dashboard = _make_dashboard_mock(dashboard_charts=[dc])
        chart_config = _make_chart_config_mock(query_config={})  # 无 datasource
        mock_dashboard_repo.get_by_id.return_value = dashboard
        mock_dashboard_chart_repo.get_chart_config.return_value = chart_config

        result = await service.get_dashboard_data(
            DASHBOARD_ID, USER_ID, use_cache=False
        )

        assert result is not None
        assert result["dashboard_id"] == str(DASHBOARD_ID)
        assert len(result["charts"]) == 1
        assert result["charts"][0]["error"] == "数据源未配置"
        mock_dashboard_chart_repo.get_chart_config.assert_called_once_with(
            dc.chart_config_id
        )

    @pytest.mark.asyncio
    async def test_get_data_chart_config_not_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试图表配置被删除时跳过该图表。"""
        dc = _make_dashboard_chart_mock()
        dashboard = _make_dashboard_mock(dashboard_charts=[dc])
        mock_dashboard_repo.get_by_id.return_value = dashboard
        mock_dashboard_chart_repo.get_chart_config.return_value = None

        result = await service.get_dashboard_data(
            DASHBOARD_ID, USER_ID, use_cache=False
        )

        assert result is not None
        assert result["charts"] == []

    @pytest.mark.asyncio
    async def test_get_data_query_engine_error(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试 QueryEngine 抛出异常时优雅降级。"""
        from app.services.query_engine import QueryEngineError

        ds_uuid = str(uuid4())
        dc = _make_dashboard_chart_mock()
        dashboard = _make_dashboard_mock(dashboard_charts=[dc])
        chart_config = _make_chart_config_mock(
            query_config={
                "datasourceId": ds_uuid,
                "dimensions": ["month"],
                "measures": [{"field": "sales", "agg": "SUM"}],
            }
        )
        mock_dashboard_repo.get_by_id.return_value = dashboard
        mock_dashboard_chart_repo.get_chart_config.return_value = chart_config

        with patch(
            "app.services.dashboard_service.execute_chart_query",
            new=AsyncMock(side_effect=QueryEngineError("表不存在")),
        ):
            result = await service.get_dashboard_data(
                DASHBOARD_ID, USER_ID, use_cache=False
            )

        assert result is not None
        assert len(result["charts"]) == 1
        assert result["charts"][0]["error"] == "表不存在"

    @pytest.mark.asyncio
    async def test_get_data_success(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_dashboard_chart_repo: AsyncMock,
    ) -> None:
        """测试正常路径：图表配置 + 查询执行都成功。"""
        ds_uuid = str(uuid4())
        dc = _make_dashboard_chart_mock()
        dashboard = _make_dashboard_mock(dashboard_charts=[dc])
        chart_config = _make_chart_config_mock(
            chart_type="bar",
            query_config={
                "datasourceId": ds_uuid,
                "dimensions": ["month"],
                "measures": [{"field": "sales", "agg": "SUM"}],
            },
            render_config={"renderer": "echarts"},
        )
        mock_dashboard_repo.get_by_id.return_value = dashboard
        mock_dashboard_chart_repo.get_chart_config.return_value = chart_config

        query_result_mock = MagicMock()
        query_result_mock.model_dump.return_value = {"rows": [{"month": "2024-01", "sales": 1000}]}

        with patch(
            "app.services.dashboard_service.execute_chart_query",
            new=AsyncMock(return_value=query_result_mock),
        ):
            result = await service.get_dashboard_data(
                DASHBOARD_ID, USER_ID, use_cache=False
            )

        assert result is not None
        assert len(result["charts"]) == 1
        chart = result["charts"][0]
        assert chart["chartId"] == str(dc.id)
        assert chart["title"] == dc.title
        assert chart["chartType"] == "bar"
        assert chart["renderConfig"] == {"renderer": "echarts"}
        assert chart["data"] == {"rows": [{"month": "2024-01", "sales": 1000}]}


# ── refresh 方法 ─────────────────────────────────────────────────────────────


class TestDashboardServiceRefresh:
    """refresh 方法测试。"""

    @pytest.mark.asyncio
    async def test_refresh_dashboard_not_found(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
    ) -> None:
        """测试仪表板不存在时返回 False。"""
        mock_dashboard_repo.get_by_id.return_value = None

        result = await service.refresh(DASHBOARD_ID, USER_ID)

        assert result is False

    @pytest.mark.asyncio
    async def test_refresh_success(
        self,
        service: DashboardService,
        mock_dashboard_repo: AsyncMock,
        mock_cache_repo: MagicMock,
    ) -> None:
        """测试刷新成功（删除缓存）。"""
        mock_dashboard_repo.get_by_id.return_value = _make_dashboard_mock()

        result = await service.refresh(DASHBOARD_ID, USER_ID)

        mock_cache_repo.delete.assert_called_once_with(f"dashboard:{DASHBOARD_ID}:data")
        assert result is True
