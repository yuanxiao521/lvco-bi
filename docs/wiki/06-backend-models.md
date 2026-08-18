# 06 · 数据模型与 E-R 图

> 全部模型位于 `app/models/`，主键均为 `UUID(as_uuid=True)`（PG 端 `uuid_generate_v4()`），时间戳来自 `TimestampMixin`（created_at/updated_at）。

## E-R 图

```mermaid
erDiagram
    users ||--o{ datasources : owns
    users ||--o{ canvases : owns
    users ||--o{ dashboards : owns
    users ||--o{ reports : owns
    users ||--o{ ai_sessions : owns
    users ||--o{ ai_messages : owns
    users ||--o{ insight_rules : owns
    users ||--o{ insight_records : owns
    users ||--o{ insight_suggestions : owns
    users ||--o{ notifications : owns
    users ||--o{ user_preferences : owns
    users ||--o{ operation_logs : audited
    datasources ||--o{ canvases : used_by
    datasources ||--o{ chart_configs : basis
    datasources ||--o{ insight_rules : monitored
    datasources ||--o{ insight_records : source
    datasources ||--o{ insight_suggestions : scanned
    dashboards ||--o{ dashboard_charts : contains
    chart_configs ||--o{ dashboard_charts : referenced
    ai_sessions ||--o{ ai_messages : has
    insight_rules ||--o{ insight_records : produces
    insight_records o|--|| reports : exported_as
    insight_suggestions o|--o| insight_rules : accepted_as

    users {
        uuid id PK
        string email UK
        string password_hash
        string display_name
        string avatar_url
        enum role "admin/editor/viewer"
    }
    datasources {
        uuid id PK
        uuid user_id FK
        string name
        enum source_type "csv/excel/mysql/postgresql"
        json connection_config
        string file_path
        json schema_meta
        enum status "connected/disconnected/syncing"
        bigint size_bytes
        int row_count
        datetime last_synced_at
    }
    canvases {
        uuid id PK
        uuid user_id FK
        uuid datasource_id FK
        string table_name
        string title
        json blocks
        datetime deleted_at
    }
    chart_configs {
        uuid id PK
        uuid datasource_id FK
        enum chart_type "14种"
        json query_config
        json render_config
    }
    dashboards {
        uuid id PK
        uuid user_id FK
        string title
        string description
        json layout
        int refresh_interval
        bool is_public
        string share_token UK
        datetime deleted_at
    }
    dashboard_charts {
        uuid id PK
        uuid dashboard_id FK
        uuid chart_config_id FK
        string title
        json position
    }
    reports {
        uuid id PK
        uuid user_id FK
        string title
        enum source_type "canvas/dashboard/manual/ai_insight"
        uuid source_id
        json snapshot_blocks
        enum status "draft/published/shared/deleted"
        string share_token UK
    }
    ai_sessions {
        uuid id PK
        uuid user_id FK
        string model
        string title
    }
    ai_messages {
        uuid id PK
        uuid session_id FK
        enum role "user/assistant"
        text content
        json chart_data
    }
    insight_rules {
        uuid id PK
        uuid user_id FK
        uuid datasource_id FK
        string name
        json query_config "table/time_field/measures/dimensions/time_range_days"
        array detect_types
        json threshold
        enum report_type "daily/weekly"
        enum schedule "daily/weekly"
        time schedule_time
        bool enabled
        bool auto_created
        datetime last_run_at
        enum last_run_status "pending/running/success/failed"
        datetime next_run_at
    }
    insight_records {
        uuid id PK
        uuid rule_id FK
        uuid user_id FK
        uuid datasource_id FK
        datetime run_at
        datetime period_start
        datetime period_end
        string status
        text error_message
        text ai_narrative
        json charts
        json raw_data
        json detected_anomalies
        string llm_model
        int llm_tokens_input
        int llm_tokens_output
        uuid report_id FK
    }
    insight_suggestions {
        uuid id PK
        uuid user_id FK
        uuid datasource_id FK
        string table_name
        string time_field
        array measure_fields
        array dimension_fields
        string suggested_name
        json suggested_config
        string rationale
        float confidence
        int row_count_estimate
        string update_frequency
        enum status "pending/accepted/dismissed"
        uuid accepted_rule_id FK
        datetime acted_at
    }
    notifications {
        uuid id PK
        uuid user_id FK
        string type "insight_ready/insight_failed/suggestion_ready/system"
        string title
        text body
        string link_url
        string resource_type
        uuid resource_id
        json metadata
        bool read
        datetime read_at
    }
    operation_logs {
        uuid id PK
        uuid user_id FK
        string action
        string resource_type
        uuid resource_id
        string method
        string path
        int status_code
        int duration_ms
        string ip_address
        string user_agent
        json extra
    }
    user_preferences {
        uuid id PK
        uuid user_id FK
        string preference_type "chart_type/color_scheme/dimension/aggregation/analysis_focus"
        string preference_key
        json preference_value
        float strength
        int evidence_count
        datetime last_used_at
    }
```

## 模型清单与要点

| 模型文件 | 表名 | 关键要点 |
|----------|------|---------|
| `user.py` | users | `UserRole` 枚举 admin/editor/viewer；关系级联删除 |
| `datasource.py` | datasources | `SourceType`(csv/excel/mysql/postgresql)、`DatasourceStatus`；connection_config 存加密连接串；schema_meta 存字段分类 |
| `canvas.py` | canvases | blocks JSON 存画布块；软删除 |
| `chart_config.py` | chart_configs | `ChartType` 枚举 **14 种**：bar/line/pie/area/scatter/funnel/radar/heatmap/sankey/horizontal_bar/grouped_bar/stacked_bar/kpi_card/donut |
| `dashboard.py` | dashboards | layout JSON、refresh_interval(300)、share_token、软删除 |
| `dashboard_chart.py` | dashboard_charts | position JSON 布局 |
| `report.py` | reports | `ReportStatus`(draft/published/shared/deleted)、`ReportSourceType`(canvas/dashboard/manual/ai_insight)；snapshot_blocks 快照 |
| `ai_session.py` | ai_sessions | model 字段（默认 gpt-4o） |
| `ai_message.py` | ai_messages | chart_data JSON 存图表 option；role 枚举 |
| `insight_rule.py` | insight_rules | `ReportType`/`ScheduleType`/`RunStatus` 枚举；部分索引 `(user_id, enabled) WHERE enabled` |
| `insight_record.py` | insight_records | 存 ai_narrative/charts/raw_data/detected_anomalies/llm tokens |
| `insight_suggestion.py` | insight_suggestions | `SuggestionStatus`；accepted_rule_id 关联 |
| `notification.py` | notifications | `NotificationType`(insight_ready/insight_failed/suggestion_ready/system) |
| `operation_log.py` | operation_logs | 审计日志（中间件写入），action/resource_type/resource_id 均建索引 |
| `user_preference.py` | user_preferences | 偏好记忆：strength(0-1)/evidence_count/衰减字段 |
| `base.py` | — | `Base` + `TimestampMixin` |

## 关系汇总

- **User 1—N**：DataSource / Canvas / Dashboard / Report / AISession / UserPreference / Notification
- **DataSource 1—N**：Canvas / ChartConfig / InsightRule / InsightRecord / InsightSuggestion
- **Dashboard 1—N** DashboardChart **N—1** ChartConfig
- **AISession 1—N** AIMessage
- **InsightRule 1—N** InsightRecord；InsightRecord **N—1** Report（report_id）；InsightSuggestion **N—1** InsightRule（accepted_rule_id）

## 注意

- `models/__init__.py` 未导出 insight_record / insight_rule / insight_suggestion 三个模型，需直接 `import` 具体模块。
- 0013 迁移建 `user_preferences` 时未建 FK 约束（模型声明了 ForeignKey，属历史遗留差异）。
- 图表类型扩展经历 4 个迁移（0003/0008/0009-horizontal/0010），见 [09-backend-connectors-migrations.md](./09-backend-connectors-migrations.md)。
