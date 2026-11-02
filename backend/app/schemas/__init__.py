from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
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
    description: str | None = Field(None, max_length=500)
    source_type: SourceType
    connection_config: dict | None = None
    file_path: str | None = None


class DataSourceUpdate(CamelModel):
    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=500)
    status: DatasourceStatus | None = None
    schema_meta: dict | None = None


class DataSourceResponse(CamelModel):
    id: UUID
    user_id: UUID
    name: str
    description: str | None
    source_type: SourceType
    # 数据库型数据源所属的连接（凭据、TLS、健康状态都在连接上）
    connection_id: UUID | None = None
    has_password: bool = False
    # 连接配置回传前端时只保留"可展示的部分"：密码字段一律剔除，仅给 has_password 标记。
    # 改造前这里是原样透出 connection_config —— 密码（历史上还是明文）会直接出现在
    # 浏览器 devtools / 抓包里，属于凭据外泄面。
    connection_config: dict | None = None
    file_path: str | None
    schema_meta: dict | None
    status: DatasourceStatus
    size_bytes: int
    row_count: int
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    @field_validator("connection_config")
    @classmethod
    def _mask_connection_secrets(cls, value: dict | None) -> dict | None:
        if not value:
            return None
        masked = {
            k: v
            for k, v in value.items()
            if k.lower() not in ("password", "pwd", "secret", "access_key", "secret_key")
        }
        masked["has_password"] = bool(value.get("password"))
        return masked


class ConnectionResponse(CamelModel):
    """数据库连接（一份凭据 → 多张表）。"""

    id: UUID
    name: str
    source_type: SourceType
    host: str
    port: int
    db_name: str
    username: str
    has_password: bool = False
    ssl_mode: str = "prefer"
    ssl_ca_configured: bool = False
    status: DatasourceStatus
    health_status: str | None = None
    health_error: str | None = None
    last_health_at: datetime | None = None
    datasource_count: int = 0
    created_at: datetime
    updated_at: datetime | None = None

    @classmethod
    def from_connection(cls, connection, datasource_count: int = 0) -> "ConnectionResponse":
        return cls(
            id=connection.id,
            name=connection.name,
            source_type=SourceType(getattr(connection.source_type, "value", connection.source_type)),
            host=connection.host,
            port=connection.port,
            db_name=connection.db_name or "",
            username=connection.username or "",
            has_password=bool(connection.password_enc),
            ssl_mode=connection.ssl_mode or "prefer",
            ssl_ca_configured=bool(connection.ssl_ca_enc),
            status=DatasourceStatus(
                getattr(connection.status, "value", connection.status) or "disconnected"
            ),
            health_status=connection.health_status,
            health_error=connection.health_error,
            last_health_at=connection.last_health_at,
            datasource_count=datasource_count,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )


class ConnectDatasourceResponse(CamelModel):
    connection: ConnectionResponse
    datasources: list["DataSourceResponse"] = []


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
    # 换数据源：画布级单一数据源，改动后旧图表的字段引用失效，由前端标记为"待重配"
    datasource_id: UUID | None = None


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
    entry: str
    canvas_id: UUID | None
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
    datasource_id: str | None = None
    session_id: str | None = None
    # resume=true 时 message 可为空：不重发消息，仅续收运行中任务（画布助手后台化）
    message: str = Field("", max_length=2000)  # 长度限制只作用于用户原始输入
    resume: bool = False  # true = 断线重连续收：跳过会话/锁/消息保存，直接订阅运行中任务
    canvas_context: dict | None = None  # blocks, current config, etc.
    canvas_id: str | None = None  # 画布 ID；用于会话按画布隔离（可为空 = 未保存草稿）
    new_session: bool | None = None  # 强制新建会话（忽略该画布最近会话），供"新对话"使用
    # HITL 确认卡片动作：{"type": "confirm" | "cancel"}。用户点击确认/取消按钮时携带，
    # 主管守卫据此短路决策直接恢复/取消（无此字段 = 自由文本，走正常决策）。
    ui_action: dict | None = None


class DataChatRequest(CamelModel):
    datasource_id: str | None = None
    session_id: str | None = None  # 会话 ID，用于保存消息
    message: str = Field(..., max_length=2000)  # 长度限制只作用于用户原始输入
    history: list[dict] | None = None
    resume: bool = False  # 续收标记：刷新/断线后重连现有后台任务（纯订阅，不落新消息）
    # HITL 确认卡片动作（同 CanvasChatRequest.ui_action）
    ui_action: dict | None = None


AggKind = Literal["SUM", "AVG", "COUNT", "MAX", "MIN", "COUNT_DISTINCT", "MEDIAN", "STDDEV"]


class MetricCreate(CamelModel):
    key: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=1000)
    # formula 与 source_field+agg 二选一：
    # - 直接写口径表达式（高级）；或
    # - 选一个数据源字段 + 聚合方式，由后端自动生成 SUM("字段") 式公式（免手写 SQL）
    formula: str | None = None
    source_field: str | None = None
    agg: AggKind | None = None
    agg_kind: str | None = Field(None, max_length=20)
    datasource_id: UUID | None = None
    table_ref: str | None = Field("data", max_length=200)


class MetricUpdate(CamelModel):
    name: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=1000)
    formula: str | None = None
    agg_kind: str | None = Field(None, max_length=20)
    datasource_id: UUID | None = None
    table_ref: str | None = Field(None, max_length=200)
    active: bool | None = None


class MetricResponse(CamelModel):
    id: UUID
    key: str
    name: str
    description: str | None
    formula: str
    formula_type: str = "basic"
    depends_on_metric_ids: list | None = None
    version: int = 1
    agg_kind: str | None
    datasource_id: UUID | None
    table_ref: str | None
    active: bool
    created_at: datetime
    updated_at: datetime | None


AISessionDetail.model_rebuild()