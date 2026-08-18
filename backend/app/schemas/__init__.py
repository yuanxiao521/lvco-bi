from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.models.chart_config import ChartConfig, ChartType
from app.models.dashboard_chart import DashboardChart
from app.models.datasource import DatasourceStatus, SourceType
from app.models.user import UserRole


class CamelModel(BaseModel):
    """项目统一 API 基础模型：snake_case ↔ camelCase 自动转换。

    - 内部 Python 代码保持 snake_case（符合 PEP 8）
    - JSON 响应/请求自动转 camelCase（符合 TypeScript/React 生态）
    - ``populate_by_name=True`` 允许前端任一命名风格提交
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="ignore",
    )


class SuccessResponse(CamelModel):
    success: bool = True
    data: dict | list | None = None


class ErrorResponse(CamelModel):
    success: bool = False
    error: dict


class PaginatedResponse(CamelModel):
    success: bool = True
    data: dict


class LoginRequest(CamelModel):
    email: str = Field(..., description="用户邮箱")
    password: str = Field(..., description="密码")


class RegisterRequest(CamelModel):
    email: str = Field(..., description="用户邮箱")
    password: str = Field(..., min_length=8, description="密码，至少8位")
    display_name: str = Field(..., description="显示名称")


class RefreshTokenRequest(CamelModel):
    refresh_token: str


class TokenResponse(CamelModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int


class UserResponse(CamelModel):
    id: UUID
    email: str
    display_name: str
    avatar_url: str | None
    role: UserRole
    created_at: datetime


class UserCreate(CamelModel):
    email: str
    password: str = Field(..., min_length=8)
    display_name: str


class UserUpdate(CamelModel):
    display_name: str | None = None
    avatar_url: str | None = None
    role: UserRole | None = None


class DataSourceCreate(CamelModel):
    name: str = Field(..., max_length=200)
    source_type: SourceType
    connection_config: dict | None = None
    file_path: str | None = None


class DataSourceUpdate(CamelModel):
    name: str | None = Field(None, max_length=200)
    status: DatasourceStatus | None = None
    schema_meta: dict | None = None


class DataSourceResponse(CamelModel):
    id: UUID
    user_id: UUID
    name: str
    source_type: SourceType
    connection_config: dict | None
    file_path: str | None
    schema_meta: dict | None
    status: DatasourceStatus
    size_bytes: int
    row_count: int
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


class DataSourceListResponse(CamelModel):
    items: list[DataSourceResponse]
    total: int
    page: int
    page_size: int
    pages: int


class CanvasCreate(CamelModel):
    title: str = Field(..., max_length=200)
    datasource_id: UUID
    table_name: str | None = None


class CanvasUpdate(CamelModel):
    title: str | None = Field(None, max_length=200)
    table_name: str | None = None


class CanvasBlocksUpdate(CamelModel):
    blocks: list


class CanvasResponse(CamelModel):
    id: UUID
    user_id: UUID
    datasource_id: UUID | None
    table_name: str | None
    title: str
    blocks: list | None
    created_at: datetime
    updated_at: datetime | None


class ChartConfigCreate(CamelModel):
    chart_type: str
    query_config: dict


class ChartConfigResponse(CamelModel):
    id: UUID
    chart_type: str
    query_config: dict
    created_at: datetime


class DashboardCreate(CamelModel):
    title: str = Field(..., max_length=200)
    description: str | None = None


class DashboardUpdate(CamelModel):
    title: str | None = Field(None, max_length=200)
    description: str | None = None
    layout: list | None = None
    refresh_interval: int | None = None
    is_public: bool | None = None


class DashboardResponse(CamelModel):
    id: UUID
    user_id: UUID
    title: str
    description: str | None
    layout: list | None
    refresh_interval: int
    is_public: bool
    share_token: str | None
    created_at: datetime
    updated_at: datetime | None


class DashboardChartCreate(CamelModel):
    chart_config_id: UUID
    title: str | None = None
    position: dict | None = None


class DashboardChartResponse(CamelModel):
    id: UUID
    dashboard_id: UUID
    chart_config_id: UUID
    title: str | None
    position: dict | None
    created_at: datetime


class ReportCreate(CamelModel):
    title: str = Field(..., max_length=200)
    source_type: str
    source_id: UUID | None = None
    snapshot_blocks: dict | None = None


class ReportUpdate(CamelModel):
    title: str | None = Field(None, max_length=200)
    status: str | None = None
    snapshot_blocks: dict | None = None


class ReportResponse(CamelModel):
    id: UUID
    user_id: UUID
    title: str
    source_type: str
    source_id: UUID | None
    snapshot_blocks: dict | None
    status: str
    share_token: str | None
    created_at: datetime
    updated_at: datetime | None


class AISessionCreate(CamelModel):
    title: str | None = Field(None, max_length=200)


class AISessionResponse(CamelModel):
    id: UUID
    user_id: UUID
    model: str
    title: str | None
    created_at: datetime


class AISessionDetail(AISessionResponse):
    messages: list["AIMessageResponse"] = Field(default_factory=list)


class AIMessageCreate(CamelModel):
    content: str = Field(..., min_length=1, max_length=10000)


class AIMessageResponse(CamelModel):
    id: UUID
    session_id: UUID
    role: str
    content: str
    chart_data: dict | None
    created_at: datetime


class AICleanRequest(CamelModel):
    datasource_id: UUID
    rules: list[dict]  # [{field: str, action: str}]  action: drop_null|drop_negative|standardize_date|fill_mean


class AICleanSuggestion(CamelModel):
    field: str
    action: str
    rationale: str


class AICleanResult(CamelModel):
    suggestions: list[AICleanSuggestion]


class AIRecommendRequest(CamelModel):
    datasource_id: UUID
    context: str | None = None


class AIRecommendChart(CamelModel):
    title: str
    config: dict[str, Any]
    rationale: str


class AIRecommendResult(CamelModel):
    suggestions: list[AIRecommendChart]


class AIQueryRequest(CamelModel):
    question: str
    datasource_id: UUID | None = None


class InsightsRequest(CamelModel):
    datasource_id: UUID
    query_config: dict


class PolishRequest(CamelModel):
    text: str
    style: str = "professional"


class CanvasChatRequest(CamelModel):
    datasource_id: str
    session_id: str
    message: str
    canvas_context: dict | None = None  # blocks, current config, etc.


class DataChatRequest(CamelModel):
    datasource_id: str | None = None
    session_id: str | None = None  # 会话 ID，用于保存消息
    message: str
    history: list[dict] | None = None


AISessionDetail.model_rebuild()