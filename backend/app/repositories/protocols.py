"""Repository Protocol 定义。

使用 Python typing.Protocol 定义接口，支持鸭子类型和 mock 测试。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class CacheRepository(Protocol):
    """缓存仓库协议。"""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


@runtime_checkable
class QueryRepository(Protocol):
    """查询仓库协议。"""

    async def execute(self, sql: str, params: dict | None = None) -> list[dict[str, Any]]: ...
    async def execute_scalar(self, sql: str, params: dict | None = None) -> Any: ...


@runtime_checkable
class StorageRepository(Protocol):
    """存储仓库协议。"""

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    async def download(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...


@runtime_checkable
class CanvasRepository(Protocol):
    """画布仓库协议。"""

    async def create(
        self,
        user_id: UUID,
        title: str,
        datasource_id: UUID | None,
        table_name: str | None = None,
    ) -> Any: ...
    async def list_canvases(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[Any], int]: ...
    async def get_by_id(self, canvas_id: UUID, user_id: UUID) -> Any | None: ...
    async def update_blocks(self, canvas_id: UUID, user_id: UUID, blocks: list[Any]) -> Any | None: ...
    async def delete(self, canvas_id: UUID, user_id: UUID) -> bool: ...
    async def update_title(self, canvas_id: UUID, user_id: UUID, title: str) -> Any | None: ...


@runtime_checkable
class ChartConfigRepository(Protocol):
    """图表配置仓库协议。"""

    async def create(
        self,
        chart_type: Any,
        query_config: dict,
        datasource_id: UUID | None = None,
        render_config: dict | None = None,
    ) -> Any: ...


@runtime_checkable
class DashboardRepository(Protocol):
    """仪表板仓库协议。"""

    async def create(
        self,
        user_id: UUID,
        title: str,
        description: str | None = None,
    ) -> Any: ...
    async def list_dashboards(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        search: str | None = None,
    ) -> tuple[list[Any], int]: ...
    async def get_by_id(self, dashboard_id: UUID, user_id: UUID) -> Any | None: ...
    async def update_layout(self, dashboard_id: UUID, user_id: UUID, layout: list[Any]) -> Any | None: ...
    async def delete(self, dashboard_id: UUID, user_id: UUID) -> bool: ...
    async def share(self, dashboard_id: UUID, user_id: UUID) -> Any | None: ...


@runtime_checkable
class DashboardChartRepository(Protocol):
    """仪表板图表仓库协议。"""

    async def add_chart(
        self,
        dashboard_id: UUID,
        chart_config_id: UUID,
        title: str | None = None,
        position: dict | None = None,
    ) -> Any: ...
    async def remove_chart(
        self,
        dashboard_id: UUID,
        chart_id: UUID,
        user_id: UUID,
    ) -> bool: ...
    async def get_chart_config(self, chart_config_id: UUID) -> Any | None: ...


@runtime_checkable
class UserPreferenceRepository(Protocol):
    """用户偏好仓库协议。"""

    async def get_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
    ) -> Any | None: ...
    async def get_user_preferences(
        self,
        user_id: UUID,
        preference_type: str | None = None,
    ) -> list[Any]: ...
    async def get_top_preferences(
        self,
        user_id: UUID,
        preference_type: str,
        limit: int = 3,
    ) -> list[Any]: ...
    async def create_preference(
        self,
        user_id: UUID,
        preference_type: str,
        preference_key: str,
        preference_value: dict,
        strength: float,
        evidence_count: int,
    ) -> Any: ...
    async def update_preference(
        self,
        preference: Any,
        preference_value: dict,
        strength: float,
        evidence_count: int,
    ) -> Any: ...
    async def delete_preference(self, preference: Any) -> None: ...


@runtime_checkable
class ReportRepository(Protocol):
    """报告仓库协议。"""

    async def create(
        self,
        user_id: UUID,
        title: str,
        source_type: str,
        source_id: UUID | None,
        snapshot_blocks: dict | None,
    ) -> Any: ...
    async def list_reports(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        source_type: str | None,
        status_filter: str | None,
    ) -> tuple[list[Any], int]: ...
    async def get_by_id(self, report_id: UUID, user_id: UUID) -> Any | None: ...
    async def update_status(self, report: Any, status: str) -> Any: ...
    async def update_title(self, report: Any, title: str) -> Any: ...
    async def share(self, report: Any) -> Any: ...
    async def delete(self, report: Any) -> None: ...


@runtime_checkable
class DataSourceRepository(Protocol):
    """数据源仓库协议。"""

    async def create(
        self,
        user_id: UUID,
        name: str,
        source_type: str,
        file_path: str | None,
        connection_config: dict | None,
        status: str,
        size_bytes: int,
    ) -> Any: ...
    async def list_datasources(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        source_type: str | None,
        status: str | None,
        search: str | None,
    ) -> tuple[list[Any], int]: ...
    async def get_by_id(self, datasource_id: UUID, user_id: UUID) -> Any | None: ...
    async def update(self, datasource: Any, **kwargs: Any) -> Any: ...
    async def delete(self, datasource: Any) -> None: ...
