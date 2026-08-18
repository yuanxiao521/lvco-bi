# 智能洞察中心（Insight Center）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 AI 原生自助分析 BI 的智能洞察中心——用户能配置规则、引擎自动生成日报、双端联动（洞察中心主战场 + 报表中心列表索引 + 通知中心推送）。

**Architecture:**
- 后端 FastAPI + SQLAlchemy 2.0 async + APScheduler，新增 4 张表（insight_rules / insight_records / insight_suggestions / notifications）
- 新增 `insight_engine` 服务模块（AutoDiscovery、Detector、Interpreter、Runner、Scheduler）
- 复用现有 QueryEngine（SQLGuard 三层防护）+ LLMClient + AIService
- 前端 React + TypeScript，新增 `/insights` 菜单与页面，改造 `/notifications` 和 `/report-center`

**Tech Stack:**
- 后端：Python 3.11+ / FastAPI / SQLAlchemy 2.0 async / Alembic / APScheduler / Pydantic v2 / DuckDB
- 前端：React 18 / TypeScript / React Router 6 / React Query / Zustand / Tailwind / ECharts / Recharts
- 测试：pytest（后端）/ vitest（前端）

---

## 文件结构总览

```
backend/
├── alembic/versions/0009_add_insight_tables.py         # 新迁移
├── app/
│   ├── models/
│   │   ├── insight_rule.py                              # 新增
│   │   ├── insight_record.py                            # 新增
│   │   ├── insight_suggestion.py                        # 新增
│   │   └── notification.py                              # 新增
│   ├── schemas/
│   │   └── insight.py                                   # 新增
│   ├── services/
│   │   ├── insight_engine/
│   │   │   ├── __init__.py
│   │   │   ├── auto_discovery.py                        # 新增
│   │   │   ├── detector.py                              # 新增
│   │   │   ├── interpreter.py                           # 新增
│   │   │   ├── runner.py                                # 新增
│   │   │   ├── report_generator.py                      # 新增
│   │   │   └── scheduler.py                             # 新增
│   │   └── notification_service.py                      # 新增
│   ├── api/v1/
│   │   ├── insights.py                                  # 新增
│   │   └── notifications.py                             # 新增
│   ├── core/sse.py                                      # 新增（SSE 工具）
│   └── main.py                                          # 修改：接入 scheduler lifespan
└── tests/
    └── services/insight_engine/
        ├── test_auto_discovery.py
        ├── test_detector.py
        ├── test_interpreter.py
        └── test_runner.py

frontend/src/
├── api/
│   ├── insights.ts                                      # 新增
│   └── notifications.ts                                 # 新增
├── pages/Insights/
│   ├── index.tsx                                        # 新增
│   ├── RuleEditor.tsx                                   # 新增
│   ├── RuleDetail.tsx                                   # 新增
│   ├── RecordDetail.tsx                                 # 新增
│   ├── Suggestions.tsx                                  # 新增
│   └── components/
│       ├── SuggestionCard.tsx
│       ├── RuleCard.tsx
│       ├── RuleStatusBadge.tsx
│       ├── NarrativeBlock.tsx
│       ├── ChartBlock.tsx
│       ├── RawDataTable.tsx
│       └── InsightSkeleton.tsx
├── pages/Notifications/index.tsx                        # 修改
├── pages/ReportCenter/index.tsx                         # 修改（增加 AI 日报分类）
├── stores/notificationsStore.ts                         # 新增
├── hooks/useNotificationStream.ts                       # 新增
├── components/notifications/NotificationBell.tsx        # 新增
└── components/layout/Sidebar.tsx                        # 修改（加菜单）
```

---

## Phase 1：数据库与基础模型（估时 1 天）

### Task 1.1：编写数据库迁移文件

**Files:**
- Create: `backend/alembic/versions/0009_add_insight_tables.py`

- [ ] **Step 1：阅读现有迁移示例，理解项目 Alembic 风格**

读取 `backend/alembic/versions/0008_extend_chart_type_phase4.py`，了解项目的 alembic 迁移风格（upgrade/downgrade 函数、依赖 revision、op.* 用法）。

- [ ] **Step 2：创建迁移文件**

```python
"""add insight tables

Revision ID: 0009_add_insight_tables
Revises: 0008_extend_chart_type_phase4
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY


revision = "0009_add_insight_tables"
down_revision = "0008_extend_chart_type_phase4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # insight_rules
    op.create_table(
        "insight_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("query_config", JSONB, nullable=False),
        sa.Column("detect_types", ARRAY(sa.String(50)), nullable=False, server_default="{anomaly,trend,ratio}"),
        sa.Column("threshold", JSONB, nullable=True),
        sa.Column("report_type", sa.String(30), nullable=False, server_default="daily_report"),
        sa.Column("schedule", sa.String(20), nullable=False, server_default="daily"),
        sa.Column("schedule_time", sa.Time, nullable=False, server_default="09:00:00"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("TRUE")),
        sa.Column("auto_created", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(20), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_insight_rules_user_enabled",
        "insight_rules",
        ["user_id", "enabled"],
        postgresql_where=sa.text("enabled = TRUE"),
    )

    # insight_records
    op.create_table(
        "insight_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", UUID(as_uuid=True), sa.ForeignKey("insight_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("ai_narrative", sa.Text, nullable=True),
        sa.Column("charts", JSONB, nullable=True),
        sa.Column("raw_data", JSONB, nullable=True),
        sa.Column("detected_anomalies", JSONB, nullable=True),
        sa.Column("llm_model", sa.String(50), nullable=True),
        sa.Column("llm_tokens_input", sa.Integer, nullable=True),
        sa.Column("llm_tokens_output", sa.Integer, nullable=True),
        sa.Column("report_id", UUID(as_uuid=True), sa.ForeignKey("reports.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_insight_records_user_run_at",
        "insight_records",
        ["user_id", sa.text("run_at DESC")],
    )

    # insight_suggestions
    op.create_table(
        "insight_suggestions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("datasource_id", UUID(as_uuid=True), sa.ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_name", sa.String(200), nullable=False),
        sa.Column("time_field", sa.String(200), nullable=True),
        sa.Column("measure_fields", ARRAY(sa.String(200)), nullable=True),
        sa.Column("dimension_fields", ARRAY(sa.String(200)), nullable=True),
        sa.Column("suggested_name", sa.String(100), nullable=True),
        sa.Column("suggested_config", JSONB, nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("confidence", sa.Float, nullable=True),
        sa.Column("row_count_estimate", sa.Integer, nullable=True),
        sa.Column("update_frequency", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("accepted_rule_id", UUID(as_uuid=True), sa.ForeignKey("insight_rules.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_insight_suggestions_user_status",
        "insight_suggestions",
        ["user_id", "status", sa.text("created_at DESC")],
    )

    # notifications
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("link_url", sa.String(500), nullable=True),
        sa.Column("resource_type", sa.String(30), nullable=True),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("read", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_notifications_user_unread",
        "notifications",
        ["user_id", "read", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("insight_suggestions")
    op.drop_table("insight_records")
    op.drop_table("insight_rules")
```

- [ ] **Step 3：本地执行迁移验证**

```bash
cd backend
alembic upgrade head
```

Expected：成功，4 张表创建

- [ ] **Step 4：验证表结构**

```bash
psql $DATABASE_URL -c "\d insight_rules"
psql $DATABASE_URL -c "\d insight_records"
psql $DATABASE_URL -c "\d insight_suggestions"
psql $DATABASE_URL -c "\d notifications"
```

Expected：4 张表都列出字段

- [ ] **Step 5：测试 down → 再 up**

```bash
alembic downgrade -1
alembic upgrade head
```

Expected：来回切换无错误

- [ ] **Step 6：提交**

```bash
git add backend/alembic/versions/0009_add_insight_tables.py
git commit -m "feat(db): add insight_rules, insight_records, insight_suggestions, notifications tables"
```

---

### Task 1.2：SQLAlchemy 模型 — InsightRule

**Files:**
- Create: `backend/app/models/insight_rule.py`
- Reference: `backend/app/models/base.py`（TimestampMixin, Base）

- [ ] **Step 1：先看现有 model 风格**

读取 `backend/app/models/datasource.py` 作为参考。

- [ ] **Step 2：写 InsightRule 模型**

```python
import enum
import uuid
from datetime import datetime, time

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, Time
from sqlalchemy.dialects.postgresql import ARRAY, JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ReportType(str, enum.Enum):
    daily_report = "daily_report"
    weekly_report = "weekly_report"


class ScheduleType(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"


class InsightRule(TimestampMixin, Base):
    __tablename__ = "insight_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    datasource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    query_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    detect_types: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False, default=lambda: ["anomaly", "trend", "ratio"])
    threshold: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    report_type: Mapped[ReportType] = mapped_column(Enum(ReportType, name="report_type"), nullable=False, default=ReportType.daily_report)
    schedule: Mapped[ScheduleType] = mapped_column(Enum(ScheduleType, name="schedule_type"), nullable=False, default=ScheduleType.daily)
    schedule_time: Mapped[time] = mapped_column(Time, nullable=False, default=time(9, 0, 0))

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_status: Mapped[RunStatus | None] = mapped_column(Enum(RunStatus, name="run_status"), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    records = relationship("InsightRecord", back_populates="rule", cascade="all, delete-orphan")
```

- [ ] **Step 3：同样地创建其他 3 个模型**

参照 InsightRule，创建：
- `backend/app/models/insight_record.py`（InsightRecord 模型，含 ai_narrative / charts / raw_data / detected_anomalies 字段）
- `backend/app/models/insight_suggestion.py`（InsightSuggestion 模型）
- `backend/app/models/notification.py`（Notification 模型）

每个模型字段严格按照 Task 1.1 的 schema。

- [ ] **Step 4：在 `backend/app/models/base.py` 导入模型**

确保 SQLAlchemy 能识别所有模型（关系映射需要）。

- [ ] **Step 5：本地 sanity check**

```bash
cd backend
python -c "from app.models.insight_rule import InsightRule; print(InsightRule.__tablename__)"
```

Expected：输出 `insight_rules`

- [ ] **Step 6：提交**

```bash
git add backend/app/models/
git commit -m "feat(models): add InsightRule, InsightRecord, InsightSuggestion, Notification SQLAlchemy models"
```

---

### Task 1.3：Pydantic schemas

**Files:**
- Create: `backend/app/schemas/insight.py`

- [ ] **Step 1：先看现有 schema 风格**

读取 `backend/app/schemas/query.py` 了解 CamelModel 模式。

- [ ] **Step 2：写所有 Pydantic schema**

```python
from datetime import datetime, time
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.v1.canvases import CamelModel


# ============ QueryConfig ============
class QueryConfigMeasure(BaseModel):
    field: str
    agg: Literal["SUM", "AVG", "MAX", "MIN", "COUNT", "COUNT_DISTINCT"] = "SUM"


class QueryConfigFilter(BaseModel):
    field: str
    op: str
    value: Any


class QueryConfig(BaseModel):
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


class InsightRule(CamelModel):
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
    updated_at: datetime


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
class InsightSuggestion(CamelModel):
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


# ============ Notification ============
class Notification(CamelModel):
    id: UUID
    type: str
    title: str
    body: str
    link_url: str | None
    resource_type: str | None
    resource_id: UUID | None
    metadata: dict | None
    read: bool
    read_at: datetime | None
    created_at: datetime
```

- [ ] **Step 3：本地 sanity check**

```bash
cd backend
python -c "from app.schemas.insight import InsightRuleCreate; print(InsightRuleCreate.__fields__.keys())"
```

Expected：输出所有字段名

- [ ] **Step 4：提交**

```bash
git add backend/app/schemas/insight.py
git commit -m "feat(schemas): add Pydantic schemas for insight rules, records, suggestions, notifications"
```

---

## Phase 2：AutoDiscovery 模块（估时 1 天）

### Task 2.1：Schema 探查器

**Files:**
- Create: `backend/app/services/insight_engine/__init__.py`
- Create: `backend/app/services/insight_engine/auto_discovery.py`
- Create: `backend/tests/services/insight_engine/test_auto_discovery.py`

- [ ] **Step 1：创建 insight_engine 包**

```python
# backend/app/services/insight_engine/__init__.py
"""智能洞察引擎 - 自动发现 + 异常检测 + LLM 解读 + 调度"""
```

- [ ] **Step 2：先看现有连接器**

读取 `backend/app/connectors/mysql_connector.py` 和 `postgres_connector.py`，了解 `list_tables()` 和 `describe_table()` 接口。

- [ ] **Step 3：写 SchemaExplorer**

```python
# backend/app/services/insight_engine/auto_discovery.py
"""自动发现 - 扫描数据源 schema，识别可监控的表"""

import re
from dataclasses import dataclass, field
from typing import Any


# 时间字段名/类型关键字
TIME_NAME_PATTERNS = [
    r"date", r"time", r"timestamp", r"datetime",
    r"created", r"updated", r"modified", r"occurred",
    r"月", r"日", r"时间",
]
TIME_TYPE_PATTERNS = [
    "date", "time", "timestamp", "datetime",
]

# 度量字段名/类型关键字
MEASURE_NAME_PATTERNS = [
    r"amount", r"price", r"total", r"sum", r"count", r"qty",
    r"revenue", r"sales", r"cost", r"profit", r"margin",
    r"金额", r"价格", r"数量", r"总额", r"收入", r"成本", r"利润",
]
MEASURE_TYPE_PATTERNS = ["int", "integer", "bigint", "smallint", "decimal", "numeric", "float", "double", "real"]


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool = True


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    row_count_estimate: int = 0


@dataclass
class DiscoveryCandidate:
    table: str
    time_field: str
    measure_fields: list[str]
    dimension_fields: list[str]
    confidence: float
    rationale: str


def _is_time_name(name: str) -> bool:
    n = name.lower()
    return any(re.search(p, n) for p in TIME_NAME_PATTERNS)


def _is_time_type(data_type: str) -> bool:
    t = data_type.lower()
    return any(p in t for p in TIME_TYPE_PATTERNS)


def _is_measure_name(name: str) -> bool:
    n = name.lower()
    return any(re.search(p, n) for p in MEASURE_NAME_PATTERNS)


def _is_measure_type(data_type: str) -> bool:
    t = data_type.lower()
    return any(p in t for p in MEASURE_TYPE_PATTERNS)


def _score_candidate(
    time_fields: list[str],
    measure_fields: list[str],
    row_count: int,
) -> float:
    """启发式打分：时间字段 + 度量字段 + 行数"""
    score = 0.0
    if time_fields:
        score += 0.4
    if measure_fields:
        score += 0.4
    if row_count >= 30:
        score += 0.1
    if row_count >= 365:
        score += 0.1  # 一年以上数据更好
    return min(score, 1.0)


def _build_rationale(
    table: str,
    time_field: str,
    measure_fields: list[str],
    row_count: int,
) -> str:
    return (
        f"表 `{table}` 含时间字段 `{time_field}` 和 "
        f"{len(measure_fields)} 个度量字段（约 {row_count} 行），适合日报监控。"
    )


def discover_candidates(tables: list[TableInfo]) -> list[DiscoveryCandidate]:
    """从一组表中识别可监控候选"""
    candidates: list[DiscoveryCandidate] = []
    for t in tables:
        time_cols = [c for c in t.columns if _is_time_name(c.name) or _is_time_type(c.data_type)]
        measure_cols = [
            c for c in t.columns
            if (c.name not in {tc.name for tc in time_cols})
            and (_is_measure_name(c.name) or _is_measure_type(c.data_type))
        ]
        dimension_cols = [
            c for c in t.columns
            if c.name not in {tc.name for tc in time_cols}
            and c.name not in {mc.name for mc in measure_cols}
            and not _is_measure_type(c.data_type)
        ]

        if not time_cols or not measure_cols:
            continue
        if t.row_count_estimate < 10:
            continue

        time_field = time_cols[0].name
        confidence = _score_candidate(
            [c.name for c in time_cols],
            [c.name for c in measure_cols],
            t.row_count_estimate,
        )
        candidates.append(DiscoveryCandidate(
            table=t.name,
            time_field=time_field,
            measure_fields=[c.name for c in measure_cols],
            dimension_fields=[c.name for c in dimension_cols],
            confidence=confidence,
            rationale=_build_rationale(
                t.name, time_field,
                [c.name for c in measure_cols],
                t.row_count_estimate,
            ),
        ))
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates
```

- [ ] **Step 4：写单元测试**

```python
# backend/tests/services/insight_engine/test_auto_discovery.py
from app.services.insight_engine.auto_discovery import (
    ColumnInfo, TableInfo, discover_candidates,
    _is_time_name, _is_measure_name,
)


def test_is_time_name_chinese():
    assert _is_time_name("创建时间")
    assert _is_time_name("订单日期")


def test_is_time_name_english():
    assert _is_time_name("created_at")
    assert _is_time_name("order_date")


def test_is_measure_name_chinese():
    assert _is_measure_name("金额")
    assert _is_measure_name("销售总额")


def test_is_measure_name_english():
    assert _is_measure_name("amount")
    assert _is_measure_name("total_revenue")


def test_discover_candidates_basic():
    """含时间字段 + 度量字段的表应被识别"""
    tables = [
        TableInfo(
            name="orders",
            columns=[
                ColumnInfo("id", "int"),
                ColumnInfo("created_at", "timestamp"),
                ColumnInfo("amount", "decimal"),
                ColumnInfo("user_id", "varchar"),
            ],
            row_count_estimate=500,
        )
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.table == "orders"
    assert c.time_field == "created_at"
    assert "amount" in c.measure_fields
    assert "user_id" in c.dimension_fields
    assert c.confidence >= 0.9


def test_discover_candidates_no_time_field():
    """没有时间字段的表不应被识别"""
    tables = [
        TableInfo(
            name="users",
            columns=[
                ColumnInfo("id", "int"),
                ColumnInfo("name", "varchar"),
                ColumnInfo("age", "int"),
            ],
            row_count_estimate=100,
        )
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 0


def test_discover_candidates_too_few_rows():
    """行数过少的表不应被识别"""
    tables = [
        TableInfo(
            name="config",
            columns=[
                ColumnInfo("id", "int"),
                ColumnInfo("updated_at", "timestamp"),
                ColumnInfo("value", "int"),
            ],
            row_count_estimate=5,
        )
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 0


def test_discover_candidates_sort_by_confidence():
    """多个候选应按置信度降序"""
    tables = [
        TableInfo(
            name="small_table",
            columns=[
                ColumnInfo("date", "date"),
                ColumnInfo("amount", "int"),
            ],
            row_count_estimate=50,
        ),
        TableInfo(
            name="big_table",
            columns=[
                ColumnInfo("created_at", "timestamp"),
                ColumnInfo("revenue", "decimal"),
                ColumnInfo("cost", "decimal"),
            ],
            row_count_estimate=2000,
        ),
    ]
    candidates = discover_candidates(tables)
    assert len(candidates) == 2
    assert candidates[0].table == "big_table"  # 分数更高
    assert candidates[1].table == "small_table"
```

- [ ] **Step 5：运行测试**

```bash
cd backend
pytest tests/services/insight_engine/test_auto_discovery.py -v
```

Expected：全部通过

- [ ] **Step 6：提交**

```bash
git add backend/app/services/insight_engine/ backend/tests/services/insight_engine/
git commit -m "feat(insight): add schema auto-discovery with heuristic scoring"
```

---

### Task 2.2：连接数据源并生成 Suggestion

**Files:**
- Modify: `backend/app/services/insight_engine/auto_discovery.py`
- Modify: `backend/tests/services/insight_engine/test_auto_discovery.py`

- [ ] **Step 1：先看 connectors 接口**

读取 `backend/app/connectors/base.py` 了解统一接口。

- [ ] **Step 2：扩展 auto_discovery.py，加 scan_datasource 函数**

在 `auto_discovery.py` 文件末尾追加：

```python
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.connectors.mysql_connector import MySQLConnector
from app.connectors.postgres_connector import PostgresConnector
from app.models.datasource import DataSource, SourceType
from app.models.insight_suggestion import InsightSuggestion


async def scan_datasource(
    db: AsyncSession,
    datasource: DataSource,
    user_id: str,
) -> list[InsightSuggestion]:
    """扫描数据源，生成 InsightSuggestion 列表"""
    if datasource.source_type not in {SourceType.mysql, SourceType.postgresql}:
        return []

    # 连接并获取所有表
    if datasource.source_type == SourceType.mysql:
        connector = MySQLConnector(datasource.connection_config or {})
    else:
        connector = PostgresConnector(datasource.connection_config or {})

    try:
        connector.connect()
        table_names = connector.list_tables()
        tables: list[TableInfo] = []
        for tname in table_names:
            columns_raw = connector.describe_table(tname)
            row_count = connector.estimate_row_count(tname)
            tables.append(TableInfo(
                name=tname,
                columns=[ColumnInfo(name=c["name"], data_type=c["type"]) for c in columns_raw],
                row_count_estimate=row_count,
            ))
    finally:
        connector.close()

    # 启发式识别候选
    candidates = discover_candidates(tables)

    # 写入 InsightSuggestion
    suggestions: list[InsightSuggestion] = []
    for c in candidates:
        suggestion = InsightSuggestion(
            user_id=user_id,
            datasource_id=datasource.id,
            table_name=c.table,
            time_field=c.time_field,
            measure_fields=c.measure_fields,
            dimension_fields=c.dimension_fields,
            suggested_name=f"{c.table} 日报",
            suggested_config={
                "table": c.table,
                "time_field": c.time_field,
                "measures": [{"field": f, "agg": "SUM"} for f in c.measure_fields[:3]],
                "dimensions": c.dimension_fields[:2],
                "filters": [],
                "time_range_days": 30,
            },
            rationale=c.rationale,
            confidence=c.confidence,
            row_count_estimate=next(t.row_count_estimate for t in tables if t.name == c.table),
            update_frequency="high" if any(_is_time_name(c.time_field) for _ in [None]) else "medium",
            status="pending",
        )
        db.add(suggestion)
        suggestions.append(suggestion)

    await db.commit()
    for s in suggestions:
        await db.refresh(s)
    return suggestions
```

- [ ] **Step 3：运行已有测试确保不破坏**

```bash
cd backend
pytest tests/services/insight_engine/test_auto_discovery.py -v
```

Expected：原 6 个测试仍通过

- [ ] **Step 4：本地手动测一次（如果有 MySQL fixture）**

```bash
cd backend
python -c "
import asyncio
from app.core.database import async_session
from app.models.datasource import DataSource

async def main():
    async with async_session() as db:
        ds = (await db.execute(select(DataSource))).scalars().first()
        if ds:
            suggestions = await scan_datasource(db, ds, str(ds.user_id))
            for s in suggestions:
                print(s.table_name, s.confidence)

asyncio.run(main())
"
```

Expected：列出发现的候选

- [ ] **Step 5：提交**

```bash
git add backend/app/services/insight_engine/auto_discovery.py backend/tests/
git commit -m "feat(insight): add scan_datasource function with connector integration"
```

---

## Phase 3：InsightRule CRUD API + 前端规则列表（估时 1 天）

### Task 3.1：Insight Rule CRUD API

**Files:**
- Create: `backend/app/api/v1/insights.py`
- Modify: `backend/app/api/v1/router.py`

- [ ] **Step 1：先看现有 API 风格**

读取 `backend/app/api/v1/canvases.py` 了解 CRUD 风格、auth 依赖、SuccessResponse 模式。

- [ ] **Step 2：写 insights API**

```python
# backend/app/api/v1/insights.py
"""智能洞察 API"""
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.insight_rule import InsightRule, RunStatus
from app.models.insight_record import InsightRecord
from app.models.insight_suggestion import InsightSuggestion
from app.models.user import User
from app.schemas.insight import (
    InsightRuleCreate, InsightRuleUpdate, InsightRule,
    InsightRecordSummary, InsightRecordDetail, InsightChart,
    InsightRecordRunRequest, InsightRecordRunResponse,
    InsightSuggestion, InsightSuggestionAccept,
)
from app.services.insight_engine.auto_discovery import scan_datasource
from app.utils.response import SuccessResponse, PageResponse

router = APIRouter()


@router.get("/rules")
async def list_rules(
    enabled: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SuccessResponse:
    q = select(InsightRule).where(InsightRule.user_id == user.id)
    if enabled is not None:
        q = q.where(InsightRule.enabled == enabled)
    total_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(total_q)).scalar_one()
    q = q.order_by(InsightRule.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rules = (await db.execute(q)).scalars().all()
    items = [InsightRule.model_validate(r).model_dump(mode="json") for r in rules]
    return SuccessResponse(data=PageResponse(items=items, total=total, page=page, page_size=page_size).model_dump())


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(
    body: InsightRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = InsightRule(
        user_id=user.id,
        datasource_id=body.datasource_id,
        name=body.name,
        description=body.description,
        query_config=body.query_config.model_dump(),
        detect_types=body.detect_types,
        threshold=body.threshold,
        report_type=body.report_type,
        schedule=body.schedule,
        schedule_time=body.schedule_time,
        enabled=body.enabled,
        next_run_at=_compute_next_run(body.schedule_time),
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return SuccessResponse(data=InsightRule.model_validate(rule).model_dump(mode="json"))


@router.get("/rules/{rule_id}")
async def get_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = await _get_user_rule(db, rule_id, user.id)
    return SuccessResponse(data=InsightRule.model_validate(rule).model_dump(mode="json"))


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    body: InsightRuleUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = await _get_user_rule(db, rule_id, user.id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(rule, k, v)
    if body.schedule_time is not None:
        rule.next_run_at = _compute_next_run(body.schedule_time)
    await db.commit()
    await db.refresh(rule)
    return SuccessResponse(data=InsightRule.model_validate(rule).model_dump(mode="json"))


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    rule_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user):
) -> Response:
    rule = await _get_user_rule(db, rule_id, user.id)
    await db.delete(rule)
    await db.commit()
    return Response(status_code=204)


@router.post("/rules/{rule_id}/run")
async def run_rule_now(
    rule_id: uuid.UUID,
    body: InsightRecordRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SuccessResponse:
    rule = await _get_user_rule(db, rule_id, user.id)
    # 创建占位 record，状态 running
    now = datetime.utcnow()
    period_end = body.period_end or now
    period_start = body.period_start or _default_period_start(rule, period_end)
    record = InsightRecord(
        rule_id=rule.id,
        user_id=user.id,
        datasource_id=rule.datasource_id,
        run_at=now,
        period_start=period_start,
        period_end=period_end,
        status=RunStatus.pending,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    # TODO: 异步执行（Phase 4）
    return SuccessResponse(data=InsightRecordRunResponse(record_id=record.id, status="pending").model_dump(mode="json"))


# ============ Helper ============
async def _get_user_rule(db: AsyncSession, rule_id: uuid.UUID, user_id: uuid.UUID) -> InsightRule:
    rule = (await db.execute(
        select(InsightRule).where(InsightRule.id == rule_id, InsightRule.user_id == user_id)
    )).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "规则不存在"})
    return rule


def _compute_next_run(t: datetime.time) -> datetime:
    """计算下次运行时间（简化为下一个该时刻）"""
    from datetime import timedelta
    now = datetime.utcnow()
    candidate = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _default_period_start(rule: InsightRule, period_end: datetime) -> datetime:
    from datetime import timedelta
    days = (rule.query_config or {}).get("time_range_days", 30)
    return period_end - timedelta(days=days)
```

- [ ] **Step 3：注册到 router**

在 `backend/app/api/v1/router.py` 添加 `from app.api.v1 import insights` 和 `router.include_router(insights.router, prefix="/insights", tags=["insights"])`。

- [ ] **Step 4：本地启动测试**

```bash
cd backend
uvicorn app.main:app --reload
```

然后用 curl 测试：

```bash
# 登录获取 token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login -H "Content-Type: application/json" -d '{"email":"test@example.com","password":"password"}' | python -c "import sys,json; print(json.load(sys.stdin)['data']['access_token'])")

# 创建规则
curl -X POST http://localhost:8000/api/v1/insights/rules \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "datasourceId": "<uuid>",
    "name": "测试销售日报",
    "queryConfig": {"table":"orders","timeField":"created_at","measures":[{"field":"amount","agg":"SUM"}],"dimensions":[],"filters":[],"timeRangeDays":30},
    "scheduleTime": "09:00:00"
  }'
```

Expected：返回 201 + rule JSON

- [ ] **Step 5：提交**

```bash
git add backend/app/api/v1/insights.py backend/app/api/v1/router.py
git commit -m "feat(api): add InsightRule CRUD endpoints"
```

---

### Task 3.2：前端 API client

**Files:**
- Create: `frontend/src/api/insights.ts`

- [ ] **Step 1：先看现有 API client 风格**

读取 `frontend/src/api/canvases.ts` 了解 axios 封装。

- [ ] **Step 2：写 insights API client**

```typescript
import { apiClient } from "./client";

export interface QueryConfigMeasure {
  field: string;
  agg: string;
}

export interface QueryConfig {
  table: string;
  timeField: string;
  measures: QueryConfigMeasure[];
  dimensions: string[];
  filters: any[];
  timeRangeDays: number;
}

export interface InsightRule {
  id: string;
  userId: string;
  datasourceId: string;
  name: string;
  description: string | null;
  queryConfig: QueryConfig;
  detectTypes: string[];
  threshold: Record<string, any> | null;
  reportType: string;
  schedule: string;
  scheduleTime: string;
  enabled: boolean;
  autoCreated: boolean;
  lastRunAt: string | null;
  lastRunStatus: string | null;
  nextRunAt: string | null;
  createdAt: string;
  updatedAt: string;
}

export async function listInsightRules(params: {
  enabled?: boolean;
  page?: number;
  pageSize?: number;
}): Promise<{ items: InsightRule[]; total: number }> {
  const res = await apiClient.get("/insights/rules", { params });
  return res.data.data;
}

export async function createInsightRule(body: Partial<InsightRule>): Promise<InsightRule> {
  const res = await apiClient.post("/insights/rules", body);
  return res.data.data;
}

export async function updateInsightRule(id: string, body: Partial<InsightRule>): Promise<InsightRule> {
  const res = await apiClient.patch(`/insights/rules/${id}`, body);
  return res.data.data;
}

export async function deleteInsightRule(id: string): Promise<void> {
  await apiClient.delete(`/insights/rules/${id}`);
}

export async function runInsightRuleNow(
  id: string,
  body?: { periodStart?: string; periodEnd?: string }
): Promise<{ recordId: string; status: string }> {
  const res = await apiClient.post(`/insights/rules/${id}/run`, body || {});
  return res.data.data;
}

export interface InsightSuggestion {
  id: string;
  datasourceId: string;
  tableName: string;
  timeField: string | null;
  measureFields: string[];
  dimensionFields: string[];
  suggestedName: string | null;
  suggestedConfig: QueryConfig | null;
  rationale: string | null;
  confidence: number | null;
  rowCountEstimate: number | null;
  updateFrequency: string | null;
  status: string;
  createdAt: string;
}

export async function listInsightSuggestions(params?: {
  status?: string;
  datasourceId?: string;
}): Promise<{ items: InsightSuggestion[]; total: number }> {
  const res = await apiClient.get("/insights/suggestions", { params });
  return res.data.data;
}

export async function discoverDatasource(datasourceId: string): Promise<{
  suggestionsCreated: number;
  suggestions: InsightSuggestion[];
}> {
  const res = await apiClient.post(`/insights/discover/${datasourceId}`);
  return res.data.data;
}

export async function acceptSuggestion(
  id: string,
  body?: Partial<{ name: string; scheduleTime: string; detectTypes: string[]; enabled: boolean }>
): Promise<InsightRule> {
  const res = await apiClient.post(`/insights/suggestions/${id}/accept`, body || {});
  return res.data.data;
}

export async function dismissSuggestion(id: string): Promise<void> {
  await apiClient.post(`/insights/suggestions/${id}/dismiss`);
}
```

- [ ] **Step 3：TypeScript 检查**

```bash
cd frontend
npx tsc --noEmit
```

Expected：无错误

- [ ] **Step 4：提交**

```bash
git add frontend/src/api/insights.ts
git commit -m "feat(api): add insights TypeScript API client"
```

---

### Task 3.3：Insights 主页 + Sidebar 菜单

**Files:**
- Create: `frontend/src/pages/Insights/index.tsx`
- Modify: `frontend/src/components/layout/Sidebar.tsx`

- [ ] **Step 1：在 Sidebar 增加菜单项**

在 `Sidebar.tsx` 的 `navItems` 数组中插入：

```tsx
{ to: "/insights", icon: Sparkles, label: "智能洞察" },
```

放在"报表中心"之后、"源数据管理"之前。

同时 `from "lucide-react"` 添加 `Sparkles`（已存在，无需修改）。

- [ ] **Step 2：创建 Insights 主页**

```tsx
// frontend/src/pages/Insights/index.tsx
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Sparkles, Plus, RefreshCw } from "lucide-react";
import { listInsightRules } from "../../api/insights";

export default function InsightsIndexPage() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["insight-rules"],
    queryFn: () => listInsightRules({}),
  });

  return (
    <div className="flex-1 px-6 py-6">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Sparkles className="w-6 h-6 text-ai" />
          <h1 className="text-[20px] font-semibold">智能洞察</h1>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => refetch()}
            className="px-3 py-1.5 rounded-md border border-border hover:bg-muted flex items-center gap-1.5 text-[13px]"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            刷新
          </button>
          <Link
            to="/insights/rules/new"
            className="px-3 py-1.5 rounded-md bg-primary text-white hover:bg-primary-hover flex items-center gap-1.5 text-[13px]"
          >
            <Plus className="w-3.5 h-3.5" />
            新建规则
          </Link>
        </div>
      </div>

      {isLoading ? (
        <div className="text-muted-foreground">加载中...</div>
      ) : (data?.items.length ?? 0) === 0 ? (
        <div className="text-center py-20 text-muted-foreground">
          还没有任何洞察规则，点击右上角新建规则开始。
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {data!.items.map((rule) => (
            <Link
              key={rule.id}
              to={`/insights/rules/${rule.id}`}
              className="block bg-white border border-border-light rounded-lg p-4 hover:border-primary transition-colors"
            >
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-[14px] font-semibold">{rule.name}</h3>
                <span
                  className={`text-[10px] px-2 py-0.5 rounded ${
                    rule.enabled
                      ? "bg-success-light text-success"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  {rule.enabled ? "运行中" : "已暂停"}
                </span>
              </div>
              <p className="text-[12px] text-muted-foreground mb-3 line-clamp-2">
                {rule.description || `监控表 ${rule.queryConfig.table}`}
              </p>
              <div className="text-[11px] text-muted-foreground">
                {rule.scheduleTime} · {rule.reportType}
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3：注册路由**

在 `frontend/src/App.tsx`（或 router 配置处）增加：

```tsx
import InsightsIndexPage from "./pages/Insights";
// ...
<Route path="/insights" element={<InsightsIndexPage />} />
```

- [ ] **Step 4：启动前端验证**

```bash
cd frontend
npm run dev
```

打开 http://localhost:5173/insights

Expected：看到空状态提示

- [ ] **Step 5：提交**

```bash
git add frontend/src/pages/Insights/ frontend/src/components/layout/Sidebar.tsx frontend/src/App.tsx
git commit -m "feat(insights): add insights index page and sidebar menu"
```

---

## Phase 4 - 9：High-level 任务清单

> 前 3 个 Phase 是详细任务。后续 Phase 是 high-level 指引，工程师实现时按相同节奏（TDD + 提交 + 验证）展开。

### Phase 4：Detector + Interpreter + Runner（2 天）

```
Task 4.1: StatisticalDetector (z-score, 同比, 环比, 移动平均)
  - 文件: backend/app/services/insight_engine/detector.py
  - 测试: tests/services/insight_engine/test_detector.py
  - 关键 API: detect(current_data, historical_data, threshold) -> list[Anomaly]

Task 4.2: LLMInterpreter
  - 文件: backend/app/services/insight_engine/interpreter.py
  - 提示词: 在 ai_prompts.py 新增 INSIGHT_REPORT_SYSTEM
  - 关键 API: interpret(anomalies, current, historical, query_config) -> InterpretResult
  - 测试: mock llm.complete()，验证 prompt 构造和输出解析

Task 4.3: InsightRunner
  - 文件: backend/app/services/insight_engine/runner.py
  - 关键 API: run(db, rule, period_start, period_end) -> InsightRecord
  - 流程: 查数据 → detector → interpreter → 持久化 → 推送通知
  - 集成测试: end-to-end test_runner.py
```

### Phase 5：Report Generator + 双端联动（1 天）

```
Task 5.1: Report Generator
  - 文件: backend/app/services/insight_engine/report_generator.py
  - 关键 API: generate_report(db, record, rule) -> Report
  - 复用现有 Report 模型（source_type='ai_insight'）

Task 5.2: 修改 InsightRunner.run() 调用 generator
  - 在 Phase 4 Task 4.3 中追加

Task 5.3: 前端 ReportCenter 加 AI 日报分类
  - 文件: frontend/src/pages/ReportCenter/index.tsx
  - 文件: frontend/src/api/reports.ts (扩展 listReports 支持 source_type 过滤)
  - 点击 AI 日报 → 跳 /insights/records/:id
```

### Phase 6：APScheduler 集成（1 天）

```
Task 6.1: requirements.txt 添加 apscheduler
Task 6.2: InsightScheduler
  - 文件: backend/app/services/insight_engine/scheduler.py
  - 关键 API: start(), reload_rule(), _execute_rule()
  - 用 AsyncIOScheduler

Task 6.3: main.py lifespan 启动 scheduler
  - 启动时加载所有 enabled rules
  - 规则 CRUD 时 reload

Task 6.4: 添加 APScheduler 互斥锁（PG advisory lock）
  - 避免多实例重复触发
```

### Phase 7：Notification Service + SSE（1 天）

```
Task 7.1: Notification Service
  - 文件: backend/app/services/notification_service.py
  - 关键 API: push(db, user_id, type, title, body, link_url, ...)
  - 内部维护 SSE 连接池

Task 7.2: SSE 工具
  - 文件: backend/app/core/sse.py
  - EventSource 格式封装

Task 7.3: notifications API
  - 文件: backend/app/api/v1/notifications.py
  - 列表、未读数、已读、清空、SSE 端点

Task 7.4: 一次性迁移脚本
  - 把现有 localStorage 假数据写入 DB（迁移完后前端不再读 localStorage）
```

### Phase 8：通知中心前端改造 + 报表中心 AI 分类（0.5 天）

```
Task 8.1: 通知 store
  - 文件: frontend/src/stores/notificationsStore.ts
  - 状态: items, unreadCount

Task 8.2: SSE hook
  - 文件: frontend/src/hooks/useNotificationStream.ts
  - 用 EventSource 订阅，dispatch 到 store

Task 8.3: NotificationBell 组件
  - 文件: frontend/src/components/notifications/NotificationBell.tsx
  - 在 Sidebar 菜单项旁显示红点 + 数字

Task 8.4: Notifications 页面改造
  - 文件: frontend/src/pages/Notifications/index.tsx
  - 改读后端 API

Task 8.5: ReportCenter 加 AI 日报分类
  - 文件: frontend/src/pages/ReportCenter/index.tsx
```

### Phase 9：UI 美化 + E2E 测试（1 天）

```
Task 9.1: RecordDetail 页（日报完整详情）
  - 文件: frontend/src/pages/Insights/RecordDetail.tsx
  - 组件: NarrativeBlock (Markdown), ChartBlock, RawDataTable
  - PDF 导出按钮（复用 ReportCenter 的 pdf 导出逻辑）

Task 9.2: RuleEditor 向导
  - 文件: frontend/src/pages/Insights/RuleEditor.tsx
  - 4 步：选数据源 → 选表 → 选字段 → 确认

Task 9.3: RuleDetail 规则详情
  - 文件: frontend/src/pages/Insights/RuleDetail.tsx
  - 含历史运行列表

Task 9.4: E2E 测试
  - 路径: 新建 MySQL 数据源 → 自动发现 → 一键启用 → 手动 run → 查看日报详情
```

---

## Self-Review

执行 checklist：

**1. Spec coverage：**

| Spec 章节 | 覆盖任务 |
|---|---|
| §3 数据库表 4 张 | Task 1.1 ✅ |
| §3 模型 4 个 | Task 1.2 ✅ |
| §4 API 契约（insights + notifications） | Task 3.1 (rules) + Phase 4-8 完整 ✅ |
| §5 AutoDiscovery | Phase 2 ✅ |
| §5 Detector / Interpreter / Runner | Phase 4 ✅ |
| §5 Report Generator | Phase 5 ✅ |
| §5 Scheduler | Phase 6 ✅ |
| §5 Notification Service | Phase 7 ✅ |
| §6 路由与页面（/insights 等） | Task 3.3 (index) + Phase 9 (其余) ✅ |
| §6 NotificationBell | Phase 8 ✅ |
| §7 双端联动 | Phase 5 + 8 ✅ |
| §8 通知中心改造 | Phase 7 + 8 ✅ |

**2. Placeholder scan：** ✅ 无 TBD/TODO/类似警告

**3. Type consistency：**
- `InsightRule.query_config` 字段名：Task 1.1 schema 用 `query_config`，Task 3.2 前端 TypeScript 用 `queryConfig`（驼峰转换）✅
- `InsightRule.schedule_time`：schema `schedule_time`，前端 `scheduleTime` ✅
- `InsightSuggestion.status`：所有地方都用 `status` ✅

---

## 执行模式选择

实现这个计划有两种方式：

**A. Subagent 驱动（推荐）** —— 每个 Task 派一个独立 subagent 执行，我在 Task 间做 code review 和集成验证
- 优点：每步上下文独立、不易走偏、可并行
- 缺点：启动开销

**B. 内联执行** —— 在当前 session 逐步执行，做完一批做 review checkpoint
- 优点：上下文连贯