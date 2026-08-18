from datetime import datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.schemas import CamelModel


# ============ QueryConfig ============
class QueryConfigMeasure(CamelModel):
    field: str
    agg: Literal["SUM", "AVG", "MAX", "MIN", "COUNT", "COUNT_DISTINCT"] = "SUM"


class QueryConfigFilter(CamelModel):
    field: str
    op: str
    value: Any


class QueryConfig(CamelModel):
    table: str = Field(..., description="表名")
    time_field: str = Field(..., description="时间字段")
    measures: list[QueryConfigMeasure] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[QueryConfigFilter] = Field(default_factory=list)
    time_range_days: int = Field(30, ge=1, le=365)


# ============ InsightRule ============
class InsightRuleCreate(CamelModel):
    datasource_id: UUID
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    query_config: QueryConfig
    detect_types: list[str] = Field(default_factory=lambda: ["anomaly", "trend", "ratio"])
    threshold: dict | None = None
    report_type: Literal["daily_report", "weekly_report"] = "daily_report"
    schedule: Literal["daily", "weekly"] = "daily"
    schedule_time: time = time(9, 0, 0)
    enabled: bool = True


class InsightRuleUpdate(CamelModel):
    name: str | None = None
    description: str | None = None
    query_config: QueryConfig | None = None
    detect_types: list[str] | None = None
    threshold: dict | None = None
    schedule_time: time | None = None
    enabled: bool | None = None


class InsightRuleResponse(CamelModel):
    id: UUID
    user_id: UUID
    datasource_id: UUID
    name: str
    description: str | None
    query_config: dict
    detect_types: list[str]
    threshold: dict | None
    report_type: str
    schedule: str
    schedule_time: time
    enabled: bool
    auto_created: bool
    last_run_at: datetime | None
    last_run_status: str | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


class InsightRuleListResponse(CamelModel):
    items: list[InsightRuleResponse]
    total: int
    page: int
    page_size: int


# ============ InsightRecord ============
class InsightRecordSummary(CamelModel):
    id: UUID
    rule_id: UUID
    rule_name: str
    run_at: datetime
    period_start: datetime
    period_end: datetime
    status: str
    has_anomalies: bool
    anomaly_count: int
    report_id: UUID | None


class InsightChart(CamelModel):
    chart_type: str
    title: str
    config: dict
    data: list[dict]


class InsightRecordDetail(CamelModel):
    id: UUID
    rule_id: UUID
    rule_name: str
    datasource_id: UUID
    run_at: datetime
    period_start: datetime
    period_end: datetime
    status: str
    error_message: str | None
    ai_narrative: str | None
    charts: list[InsightChart]
    raw_data: list[dict]
    detected_anomalies: list[dict]
    llm_tokens_input: int | None
    llm_tokens_output: int | None


class InsightRecordRunRequest(CamelModel):
    period_start: datetime | None = None
    period_end: datetime | None = None


class InsightRecordRunResponse(CamelModel):
    record_id: UUID
    status: str


# ============ InsightSuggestion ============
class InsightSuggestionResponse(CamelModel):
    id: UUID
    datasource_id: UUID
    table_name: str
    time_field: str | None
    measure_fields: list[str]
    dimension_fields: list[str]
    suggested_name: str | None
    suggested_config: dict | None
    rationale: str | None
    confidence: float | None
    row_count_estimate: int | None
    update_frequency: str | None
    status: str
    created_at: datetime


class InsightSuggestionAccept(CamelModel):
    name: str | None = None
    schedule_time: time | None = None
    detect_types: list[str] | None = None
    enabled: bool = True


class InsightSuggestionListResponse(CamelModel):
    items: list[InsightSuggestionResponse]
    total: int


# ============ Notification ============
class NotificationResponse(CamelModel):
    id: UUID
    type: str
    title: str
    body: str
    link_url: str | None
    resource_type: str | None
    resource_id: UUID | None
    # IMPORTANT: the ORM model uses `metadata_` attribute name (because `metadata` is reserved
    # by SQLAlchemy). Pydantic's from_attributes will read `obj.metadata_`. Field name kept as
    # `metadata_` so it maps cleanly; to_camel produces `metadata` in JSON output.
    metadata_: dict | None = Field(default=None, alias="metadata")
    read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(CamelModel):
    items: list[NotificationResponse]
    total: int
    unread_count: int
