# 智能洞察中心（Insight Center）— 设计文档

> **日期**：2026-07-27
> **作者**：Lvco BI 团队
> **状态**：待用户审核
> **范围**：Phase 1 实现（智能洞察中心 + AI 助手增强延后讨论）

---

## 一、目标与范围

### 1.1 业务目标

让用户能够：
1. **自动识别** MySQL / PostgreSQL 数据源中"值得监控"的表/字段
2. **一键启用**日报监控，无需手动配置复杂 SQL
3. **每天自动生成** AI 数据日报，含 AI 叙事、图表、原始数据
4. **从报表中心**点击查看日报详情（跳转洞察中心）
5. **从通知中心**收到推送，点击查看日报

### 1.2 范围（本次）

**包含**：
- 后端 3 张新表（`insight_rules`、`insight_records`、`insight_suggestions`、`notifications`）
- 后端 `insight_engine` 模块（AutoDiscovery、Detector、Interpreter、Runner、Scheduler）
- 后端 `notification_service`
- 后端 `/api/v1/insights/*` 与 `/api/v1/notifications/*` API
- 前端 `/insights` 菜单与页面（规则列表、最近日报、建议卡片）
- 前端 `/insights/records/:id` 日报详情（**完整查看主战场**）
- 前端通知中心改造（从 localStorage 改为读后端）
- 前端报表中心 AI 日报分类（**缩略列表**，点击跳洞察中心）
- 前端 SSE 实时推送通知
- APScheduler 后台调度

**不包含（延后讨论）**：
- AI 助手整体优化（多轮记忆/错误自愈/智能追问）
- 周报模板（先做日报）
- 跨源洞察

### 1.3 非目标（明确不做）

- ❌ CSV / Excel 数据源的洞察监控（静态数据，无需监控）
- ❌ 跨数据源关联分析
- ❌ 自定义 SQL 洞察规则（先做向导式，不做 SQL 编辑器）
- ❌ 邮件 / 钉钉 / 飞书 等外部推送（先站内通知）

---

## 二、架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户操作层                                │
│  /insights  /insights/rules/:id  /insights/records/:id            │
│  /notifications                  /report-center                   │
└─────────────────────────────────────────────────────────────────┘
        │ SSE 推送                     │ HTTP
        ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       前端 React + TypeScript                    │
│  • React Router  • React Query  • Zustand  • ECharts              │
│  • SSE Client (useNotificationStream)                             │
│  • Markdown 渲染 (react-markdown)                                 │
└─────────────────────────────────────────────────────────────────┘
        │                              │
        ▼                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    后端 FastAPI (Python 3.11+)                    │
│                                                                  │
│   /api/v1/insights/*      /api/v1/notifications/*                │
│        │                          │                              │
│        ▼                          ▼                              │
│  ┌──────────────────┐      ┌──────────────────┐                  │
│  │ Insight Engine   │      │ Notification Svc │                  │
│  │ ┌──────────────┐ │      │ • DB CRUD        │                  │
│  │ │ AutoDiscovery│ │      │ • SSE 推送       │                  │
│  │ │ Detector     │ │      └──────────────────┘                  │
│  │ │ Interpreter  │ │                                          │
│  │ │ Runner       │ │      ┌──────────────────┐                  │
│  │ │ Scheduler    │ │      │ Report Center    │                  │
│  │ └──────────────┘ │      │ (复用现有)       │                  │
│  └──────────────────┘      └──────────────────┘                  │
│                                                                  │
│   AIService / LLMClient / SQLGuard / QueryEngine (现有)          │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│                  PostgreSQL + DuckDB                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、数据库设计

### 3.1 表结构

```sql
-- ============ 1. 洞察规则 ============
CREATE TABLE insight_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,

    name VARCHAR(100) NOT NULL,
    description TEXT,

    -- 查询配置
    -- {
    --   "table": "orders",
    --   "time_field": "created_at",
    --   "measures": [{"field": "amount", "agg": "SUM"}],
    --   "dimensions": [],
    --   "filters": [],
    --   "time_range_days": 30  -- 对比窗口
    -- }
    query_config JSONB NOT NULL,

    -- 检测类型: anomaly / trend / ratio / opportunity
    detect_types VARCHAR(50)[] NOT NULL DEFAULT '{anomaly,trend,ratio}',

    -- 阈值: {"z_score": 2.0, "yoy_pct": 0.2, "wow_pct": 0.15, "min_row_count": 30}
    threshold JSONB,

    -- 报告类型
    report_type VARCHAR(30) NOT NULL DEFAULT 'daily_report',

    -- 调度
    schedule VARCHAR(20) NOT NULL DEFAULT 'daily',  -- daily / weekly（先 daily）
    schedule_time TIME NOT NULL DEFAULT '09:00:00',

    enabled BOOLEAN DEFAULT TRUE,
    auto_created BOOLEAN DEFAULT FALSE,

    last_run_at TIMESTAMPTZ,
    last_run_status VARCHAR(20),  -- success / failed
    next_run_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_insight_rules_user_enabled
    ON insight_rules(user_id, enabled)
    WHERE enabled = TRUE;

CREATE INDEX idx_insight_rules_next_run
    ON insight_rules(next_run_at)
    WHERE enabled = TRUE;

-- ============ 2. 洞察记录（每次运行结果） ============
CREATE TABLE insight_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id UUID NOT NULL REFERENCES insight_rules(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,

    -- 时间窗口
    run_at TIMESTAMPTZ NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,  -- 数据周期起点
    period_end TIMESTAMPTZ NOT NULL,    -- 数据周期终点

    -- 状态
    status VARCHAR(20) NOT NULL,  -- pending / running / success / failed
    error_message TEXT,

    -- ⭐ AI 叙事（LLM 生成的 Markdown 报告文本）
    ai_narrative TEXT,

    -- ⭐ 图表配置：[{chart_type, title, config, data}]
    charts JSONB,

    -- ⭐ 原始数据（透视后的结构化数据）
    raw_data JSONB,

    -- 统计检测结果
    -- [{type: "anomaly", field: "amount", value: 12345, z_score: 2.5, severity: "warning"}]
    detected_anomalies JSONB,

    -- LLM 用量
    llm_model VARCHAR(50),
    llm_tokens_input INT,
    llm_tokens_output INT,

    -- 关联 report（用于双端联动）
    report_id UUID REFERENCES reports(id) ON DELETE SET NULL,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_insight_records_user_run_at
    ON insight_records(user_id, run_at DESC);

CREATE INDEX idx_insight_records_rule
    ON insight_records(rule_id, run_at DESC);

-- ============ 3. 自动发现建议 ============
CREATE TABLE insight_suggestions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    datasource_id UUID NOT NULL REFERENCES datasources(id) ON DELETE CASCADE,

    table_name VARCHAR(200) NOT NULL,
    time_field VARCHAR(200),
    measure_fields VARCHAR(200)[],
    dimension_fields VARCHAR(200)[],

    suggested_name VARCHAR(100),
    suggested_config JSONB,
    rationale TEXT,  -- "订单表 + 金额字段，约 120 行/天，适合日报"

    confidence FLOAT,  -- 0-1
    row_count_estimate INT,
    update_frequency VARCHAR(20),  -- high / medium / low

    status VARCHAR(20) DEFAULT 'pending',  -- pending / accepted / dismissed
    accepted_rule_id UUID REFERENCES insight_rules(id) ON DELETE SET NULL,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    acted_at TIMESTAMPTZ
);

CREATE INDEX idx_insight_suggestions_user_status
    ON insight_suggestions(user_id, status, created_at DESC);

-- ============ 4. 通知 ============
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    type VARCHAR(30) NOT NULL,  -- ai_insight / data_alert / system / collaboration
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,

    -- 跳转链接（前端路由）
    link_url VARCHAR(500),
    -- 关联资源
    resource_type VARCHAR(30),  -- insight_record / canvas / dashboard
    resource_id UUID,

    metadata JSONB,

    read BOOLEAN DEFAULT FALSE,
    read_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_notifications_user_unread
    ON notifications(user_id, read, created_at DESC);
```

### 3.2 表关系

```
users ──┬── insight_rules ── insight_records
        ├── insight_suggestions
        └── notifications

datasources ──┬── insight_rules
              ├── insight_suggestions
              └── insight_records
```

---

## 四、API 契约

### 4.1 洞察规则

```
GET    /api/v1/insights/rules
       Query: ?enabled=true&page=1&page_size=20
       Response: {items: [InsightRule], total: int}

POST   /api/v1/insights/rules
       Body: InsightRuleCreate
       Response: InsightRule

GET    /api/v1/insights/rules/{id}
       Response: InsightRule (含最近 5 条记录摘要)

PATCH  /api/v1/insights/rules/{id}
       Body: InsightRuleUpdate
       Response: InsightRule

DELETE /api/v1/insights/rules/{id}
       Response: 204

POST   /api/v1/insights/rules/{id}/run
       Body: {period_start?: ISO, period_end?: ISO}
       Response: {record_id: UUID, status: "pending"}
       Note: 触发立即运行，异步返回
```

### 4.2 洞察记录

```
GET    /api/v1/insights/records
       Query: ?rule_id=&page=&page_size=
       Response: {items: [InsightRecordSummary], total}

GET    /api/v1/insights/records/{id}
       Response: InsightRecordDetail {
         id, rule_id, run_at, period_start, period_end,
         status, ai_narrative, charts, raw_data,
         detected_anomalies, llm_tokens_input/output,
         rule: {id, name, datasource_id}
       }

GET    /api/v1/insights/records/{id}/export
       Response: PDF (Content-Type: application/pdf)
```

### 4.3 自动发现建议

```
GET    /api/v1/insights/suggestions
       Query: ?status=pending&datasource_id=
       Response: {items: [InsightSuggestion], total}

POST   /api/v1/insights/discover/{datasource_id}
       Body: {}
       Response: {suggestions_created: int, suggestions: [InsightSuggestion]}
       Note: 手动触发扫描（已有数据源变更时可用）

POST   /api/v1/insights/suggestions/{id}/accept
       Body: {name?, schedule_time?, detect_types?}
       Response: InsightRule (新建的规则)

POST   /api/v1/insights/suggestions/{id}/dismiss
       Body: {reason?: string}
       Response: 204
```

### 4.4 通知

```
GET    /api/v1/notifications
       Query: ?read=false&page=&page_size=
       Response: {items: [Notification], total, unread_count}

GET    /api/v1/notifications/unread-count
       Response: {count: int}

PATCH  /api/v1/notifications/{id}/read
       Response: Notification

PATCH  /api/v1/notifications/read-all
       Response: {updated_count: int}

DELETE /api/v1/notifications/{id}
       Response: 204

DELETE /api/v1/notifications?read_only=true
       Response: {deleted_count: int}

GET    /api/v1/notifications/stream
       Response: text/event-stream
       Events: notification.created, notification.read, etc.
```

### 4.5 数据 schema

```python
# InsightRule
{
    "id": UUID,
    "user_id": UUID,
    "datasource_id": UUID,
    "name": str,
    "description": str | None,
    "query_config": {
        "table": str,
        "time_field": str,
        "measures": [{"field": str, "agg": str}],
        "dimensions": [str],
        "filters": [{"field": str, "op": str, "value": Any}],
        "time_range_days": int
    },
    "detect_types": [str],
    "threshold": {...} | None,
    "report_type": "daily_report",
    "schedule": "daily",
    "schedule_time": "09:00:00",
    "enabled": bool,
    "auto_created": bool,
    "last_run_at": ISO | None,
    "last_run_status": str | None,
    "next_run_at": ISO | None,
    "created_at": ISO,
    "updated_at": ISO
}

# InsightRecord (Summary)
{
    "id": UUID,
    "rule_id": UUID,
    "rule_name": str,
    "run_at": ISO,
    "period_start": ISO,
    "period_end": ISO,
    "status": str,
    "has_anomalies": bool,
    "anomaly_count": int,
    "report_id": UUID | None
}

# InsightRecord (Detail) - extends Summary
{
    ...summary,
    "ai_narrative": str | None,  # Markdown
    "charts": [
        {
            "chart_type": "bar|line|pie|...",
            "title": str,
            "config": {...},
            "data": [...]
        }
    ],
    "raw_data": [...],
    "detected_anomalies": [...],
    "llm_tokens_input": int,
    "llm_tokens_output": int
}

# InsightSuggestion
{
    "id": UUID,
    "datasource_id": UUID,
    "table_name": str,
    "time_field": str | None,
    "measure_fields": [str],
    "dimension_fields": [str],
    "suggested_name": str,
    "suggested_config": {...},
    "rationale": str,
    "confidence": float,
    "row_count_estimate": int,
    "update_frequency": str,
    "status": "pending|accepted|dismissed",
    "created_at": ISO
}

# Notification
{
    "id": UUID,
    "type": "ai_insight|data_alert|system|collaboration",
    "title": str,
    "body": str,
    "link_url": str | None,
    "resource_type": str | None,
    "resource_id": UUID | None,
    "metadata": dict | None,
    "read": bool,
    "read_at": ISO | None,
    "created_at": ISO
}
```

---

## 五、后端模块设计

### 5.1 目录结构

```
backend/app/
├── services/
│   ├── insight_engine/
│   │   ├── __init__.py
│   │   ├── auto_discovery.py       # 扫描 DB schema
│   │   ├── detector.py             # 统计检测
│   │   ├── interpreter.py          # LLM 解读
│   │   ├── runner.py               # 编排：查数据 → 检测 → 解读 → 持久化
│   │   ├── report_generator.py     # 生成 report 实体（双端联动）
│   │   └── scheduler.py            # APScheduler 集成
│   └── notification_service.py
├── models/
│   ├── insight_rule.py
│   ├── insight_record.py
│   ├── insight_suggestion.py
│   └── notification.py
├── schemas/
│   └── insight.py
├── api/v1/
│   ├── insights.py
│   └── notifications.py
└── main.py  (修改：接入 scheduler lifespan)
```

### 5.2 关键模块设计

#### AutoDiscovery（自动发现）

```python
async def discover_datasource(db, datasource_id, user_id) -> list[InsightSuggestion]:
    """
    1. 连接数据源（MySQL/PG）
    2. 列出所有表（排除系统表）
    3. 对每个表：
       - 获取列元信息
       - 识别时间字段（含 date/time/timestamp 关键字或类型）
       - 识别度量字段（数值类型）
       - 估算行数（SHOW TABLE STATUS 或 pg_stat）
       - 评估数据更新频率（采样查询）
    4. 启发式打分：
       - 有时间字段 + 有度量字段 + 行数 > 30 = 高置信度
       - 包含大写/小写金额、数量、收入、订单等关键词 = 加分
    5. 转换为 InsightSuggestion 写入 DB
    """
```

#### Detector（统计检测）

```python
class StatisticalDetector:
    """对查询结果做统计异常检测"""
    
    async def detect(
        self,
        current_data: list[dict],
        historical_data: list[dict],
        threshold: ThresholdConfig
    ) -> list[Anomaly]:
        """
        检测类型：
        1. anomaly - Z-Score：当前值偏离历史均值 > threshold.z_score 个标准差
        2. trend - 移动平均：连续 N 天单调上升/下降
        3. ratio - 同/环比：(current - prev) / prev 超过 threshold.yoy_pct
        4. opportunity - 找出表现异常好的项目（top z-score）
        """
```

#### Interpreter（LLM 解读）

```python
class LLMInterpreter:
    """基于统计异常 + 数据上下文，调用 LLM 生成 narrative + 图表"""
    
    async def interpret(
        self,
        anomalies: list[Anomaly],
        current_data: list[dict],
        historical_data: list[dict],
        query_config: QueryConfig
    ) -> InterpretResult:
        """
        1. 构造 prompt：异常点 + TOP5 数据 + 历史对比
        2. 调用 LLM 生成 narrative（Markdown）+ 图表规划
        3. 解析 JSON 输出
        4. 返回 InterpretResult {narrative, charts}
        """
```

#### Runner（编排）

```python
class InsightRunner:
    """单次洞察运行的完整编排"""
    
    async def run(
        self,
        db: AsyncSession,
        rule: InsightRule,
        period_start: datetime,
        period_end: datetime
    ) -> InsightRecord:
        """
        1. 创建 InsightRecord (status=running)
        2. QueryEngine 查询 current_period 数据
        3. QueryEngine 查询 historical 数据（默认 30 天）
        4. Detector 跑异常检测
        5. Interpreter 调用 LLM 生成 narrative
        6. 生成 report 实体（双端联动）
        7. 更新 InsightRecord (status=success, narrative, charts, raw_data)
        8. NotificationService.push(...)
        9. 返回 record
        """
```

#### Scheduler（APScheduler 集成）

```python
class InsightScheduler:
    """APScheduler 封装"""

    async def start(self):
        """启动时扫描所有 enabled rules，注册 cron job"""

    async def reload_rule(self, rule_id: UUID):
        """规则增删改后重新加载调度"""

    async def _execute_rule(self, rule_id: UUID):
        """定时触发：调用 Runner.run()"""
```

### 5.3 提示词设计

```
SYSTEM:
你是资深业务分析师。基于用户提供的【当前周期数据】【历史对比数据】【统计异常点】，
生成一份中文日报，要求：

1. 顶部 AI 叙事（Markdown）：
   - 3-5 段，开头用 > 引用块给出【一句话核心结论】
   - 重点引用 TOP 1 数据和异常点
   - 给出业务解读（为什么涨/跌、可能原因）
   - 给出建议行动

2. 图表规划（JSON 数组）：
   - 2-4 个图表，覆盖：当前趋势、同环比对比、TOP 项目、异常点
   - 每个图表含 type/title/config/data
   - data 必须是透视后的结构化数据

3. 输出严格 JSON：
{
  "narrative": "<Markdown 文本>",
  "charts": [
    {
      "chart_type": "bar|line|pie|grouped_bar|...",
      "title": "中文标题",
      "config": {...},
      "data": [...]
    }
  ]
}

USER:
## 当前周期数据
{current_data_json}

## 历史对比数据（30 天）
{historical_data_json}

## 统计异常点
{anomalies_json}

请基于以上数据生成今日日报。
```

---

## 六、前端设计

### 6.1 路由与页面

```
/insights                                # 智能洞察中心首页
/insights/rules/new                      # 新建规则向导
/insights/rules/:id                      # 规则详情（编辑 + 运行历史）
/insights/rules/:id/records/:recordId    # 单次运行详情（从规则页进入）
/insights/suggestions                    # 自动发现建议列表
/insights/records/:id                    # 日报详情（主战场，可被通知中心跳转）
/notifications                           # 通知中心（改造）
/report-center                           # 报表中心（增加 AI 日报分类）
```

### 6.2 页面布局

**/insights 首页**：

```
┌─────────────────────────────────────────────────────────────┐
│ 智能洞察                       [+ 新建规则] [🔍 重新扫描]   │
├─────────────────────────────────────────────────────────────┤
│ ┌─Tab─┬─Tab─┬─Tab─┐                                         │
│ │ 规则 │ 日报 │ 建议(3)│                                     │
│ └────┴────┴────┘                                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  规则列表（日报 tab 选中时）：                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                    │
│  │ 销售日报  │ │ 用户日报  │ │ 库存日报  │  ← 卡片网格      │
│  │ ✓ 运行中 │ │ ⏸ 已暂停  │ │ ✓ 运行中  │                   │
│  │ 09:00    │ │ 09:00     │ │ 09:00     │                  │
│  │ 最近: OK │ │ 最近: --  │ │ 最近: 异常│                  │
│  └──────────┘ └──────────┘ └──────────┘                    │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**/insights/records/:id 日报详情（主战场）**：

```
┌─────────────────────────────────────────────────────────────┐
│ ← 返回    销售日报   2026-07-27 09:00  [📥 导出PDF] [🔗 分享] │
├─────────────────────────────────────────────────────────────┤
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 📊 AI 叙事                                               │ │
│ │                                                          │ │
│ │ > 今日销售额 ¥123,456，同比增长 12.3%。...                │ │
│ │                                                          │ │
│ │ ## 关键发现                                               │ │
│ │ - **杭州门店**销售异常增长...                              │ │
│ │ - 转化率下降 5%...                                       │ │
│ │                                                          │ │
│ │ ## 建议行动                                               │ │
│ │ - 关注华东库存周转                                        │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                              │
│ ┌─────────────────┐ ┌─────────────────┐                    │
│ │ 销售趋势（折线） │ │ 区域对比（柱）   │                    │
│ │   📈             │ │   📊             │                    │
│ └─────────────────┘ └─────────────────┘                    │
│ ┌─────────────────┐ ┌─────────────────┐                    │
│ │ TOP 10 商品     │ │ 异常点散点       │                    │
│ │   🏆             │ │   ⚠️             │                    │
│ └─────────────────┘ └─────────────────┘                    │
│                                                              │
│ ▼ 原始数据（点击展开）                                       │
│ ┌─────────────────────────────────────────┐                 │
│ │ 日期      │ 销售额  │ 订单数 │ 客单价     │                │
│ │ 07-26    │ 12,345 │ 234   │ 52.7       │                │
│ │ ...                                         │                │
│ └─────────────────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────┘
```

**报表中心 AI 日报分类（缩略）**：

```
/report-center 页面，tabs:
  - 我的报表
  - AI 日报  ← 新分类
    ┌────────┐ ┌────────┐ ┌────────┐
    │销售日报 │ │用户日报 │ │库存日报 │
    │07-27   │ │07-27    │ │07-26   │
    │[查看]   │ │[查看]   │ │[查看]   │
    └────────┘ └────────┘ └────────┘
    点击 → 跳到 /insights/records/:id
```

### 6.3 组件清单

```
src/
├── pages/Insights/
│   ├── index.tsx              # 主页（Tab 切换）
│   ├── RuleEditor.tsx         # 规则编辑向导（4 步：选数据源→选表→选字段→确认）
│   ├── RuleDetail.tsx         # 规则详情（含历史运行）
│   ├── RecordDetail.tsx       # 日报详情（主战场）
│   ├── Suggestions.tsx        # 建议列表
│   └── components/
│       ├── SuggestionCard.tsx     # 单条建议卡片
│       ├── RuleCard.tsx           # 规则卡片
│       ├── RuleStatusBadge.tsx    # 运行状态
│       ├── NarrativeBlock.tsx     # AI 叙事渲染
│       ├── ChartBlock.tsx         # 单个图表
│       ├── RawDataTable.tsx       # 原始数据表格
│       └── InsightSkeleton.tsx    # 加载骨架屏
├── api/
│   ├── insights.ts            # 洞察 API
│   └── notifications.ts       # 通知 API
├── stores/
│   └── notificationsStore.ts  # 通知 store（含未读数）
├── hooks/
│   └── useNotificationStream.ts  # SSE 订阅
└── components/notifications/
    └── NotificationBell.tsx   # 顶栏角标（侧栏菜单）
```

### 6.4 UI 风格约束

- 沿用现有色系：主色 `#F97316` 暖橙 / 强调色 `#10B981` 绿 / 警示色 `#EF4444` 红
- 卡片样式：圆角 8px、浅边框、悬停轻投影
- AI 叙事渲染：Markdown，左侧色块强调引用
- 图表：复用现有 ChartRenderer
- 通知角标：右上角红色圆点 + 数字

---

## 七、双端联动详细

### 7.1 数据流向

```
InsightRunner.run() 完成
       │
       ├──→ InsightRecord (status=success)
       │
       ├──→ Report (source_type='ai_insight', 自动生成)
       │     • title: "{rule.name} - {period_end 格式化}"
       │     • type: 'daily_report'
       │     • blocks: [{type: 'narrative', content: ai_narrative},
       │                 {type: 'chart', chart_id: ...}]
       │     • metadata: {insight_record_id, rule_id}
       │
       └──→ NotificationService.push(user_id,
              type='ai_insight',
              title='{rule.name}',
              body='{narrative 摘要 80 字}',
              link_url='/insights/records/{record_id}',
              resource_type='insight_record',
              resource_id=record_id)
```

### 7.2 跳转路径

| 入口 | 跳转目标 | 备注 |
|---|---|---|
| 通知中心 → 点击日报通知 | `/insights/records/:id` | **完整详情**，带 AI 叙事 |
| 报表中心 → 点击 AI 日报卡片 | `/insights/records/:id` | **完整详情**，带 AI 叙事 |
| 洞察中心 → 规则 → 历史运行 | `/insights/rules/:id/records/:recordId` | 完整详情 |
| 洞察中心首页 → 日报 tab | `/insights` (Tab=日报) | 缩略列表 |

---

## 八、通知中心改造

### 8.1 数据迁移

- 旧版：`localStorage("lvco_notifications")` 假数据
- 新版：读后端 `/api/v1/notifications`
- 一次性迁移：把 localStorage 中的种子数据写入 DB（一次性脚本）

### 8.2 实时推送

- SSE 端点 `/api/v1/notifications/stream`
- 推送事件：`notification.created`、`notification.updated`、`notification.deleted`
- 前端用 `EventSource` 订阅，存入 Zustand store

### 8.3 角标

- 侧栏菜单项旁显示红点 + 数字
- WebSocket/SSE 收到 `notification.created` 时 +1
- 用户点击进入后调用 `/read-all` 清零

---

## 九、安全与权限

### 9.1 数据隔离

- 所有查询强制带 `user_id` 过滤
- 规则、记录、建议、通知 都属于用户
- 数据源访问：复用现有授权（用户只能访问自己的数据源）

### 9.2 SQL 安全

- 洞察查询走 QueryEngine（已有 SQLGuard 三层防护）
- 不允许用户直接写 SQL 规则（向导式配置避免 SQL 注入）

### 9.3 调度安全

- APScheduler 任务在应用启动时扫描 enabled rules
- 任务执行时鉴权（user_id 必须匹配）
- 失败任务重试 3 次后标记 failed
- 异常错误写日志，不影响其他规则

---

## 十、性能考虑

### 10.1 查询优化

- DuckDB 缓存数据源元信息
- 历史数据查询用聚合（按时间分桶）
- 单次洞察运行总耗时预期 < 30s（含 LLM）

### 10.2 LLM 限流

- 调度任务错峰（同一用户多个规则间隔 30s）
- LLM 调用失败重试 2 次
- 超时 60s 自动放弃，记录 failed

### 10.3 通知推送

- 同一用户每天最多 20 条洞察通知（合并超出）
- SSE 连接每用户限 1 个

---

## 十一、阶段拆分（实现顺序）

| Phase | 内容 | 验收 | 估时 |
|---|---|---|---|
| 1.1 | 数据库迁移（3 张新表） | Alembic 迁移成功 | 0.5 天 |
| 1.2 | SQLAlchemy 模型 + Pydantic schema | 模型可读写 | 0.5 天 |
| 2 | AutoDiscovery 模块 | 上传 DB 后能扫描并生成建议 | 1 天 |
| 3 | Rule CRUD API + 前端规则列表 | 用户能手动建规则 | 1 天 |
| 4 | Detector + Interpreter + Runner | 手动 run 能生成完整日报 | 2 天 |
| 5 | Report Generator + 双端联动 | 报表中心能看到 AI 日报 | 1 天 |
| 6 | APScheduler 接入 | 每天 09:00 自动触发 | 1 天 |
| 7 | Notification Service + SSE | 通知中心能实时推送 | 1 天 |
| 8 | 通知中心前端改造 | 旧版 localStorage 替换为后端 | 0.5 天 |
| 9 | UI 美化 + E2E 测试 | 整体美观、能演示 | 1 天 |
| **合计** | | | **8.5 天** |

---

## 十二、风险与权衡

| 风险 | 影响 | 缓解策略 |
|---|---|---|
| LLM 解读质量不稳定 | 日报内容质量差 | 提示词持续优化；保留规则引擎 fallback |
| APScheduler 重启后任务丢失 | 错过当天的日报 | 启动时扫描所有 overdue rules 补跑 |
| 多实例部署时任务重复 | 重复生成日报 | 用 PostgreSQL advisory lock 互斥 |
| 大数据源扫描慢 | 用户体验差 | 后台异步扫描 + 进度通知 |
| 通知推送量大 | 打扰用户 | 合并策略 + 用户可配置阈值 |

---

## 十三、测试策略

### 13.1 单元测试

- AutoDiscovery：测试表识别逻辑
- Detector：测试 z-score、同环比计算
- Interpreter：mock LLM，验证 prompt 构造和输出解析
- Runner：mock QueryEngine，验证编排

### 13.2 集成测试

- 完整 run 流程：建规则 → 触发 → 生成记录 → 推送通知
- 双端联动：报表中心能查到 AI 日报

### 13.3 E2E 测试

- 用户新建 MySQL 数据源 → 自动发现 → 一键启用 → 等待或手动触发 → 查看日报详情

---

## 十四、待用户确认

请确认以下几点后开始实现：

1. ✅ **范围**：Insight Center 全部 + 通知中心改造 + 报表中心 AI 日报分类
2. ✅ **数据源**：仅 MySQL / PostgreSQL
3. ✅ **报告类型**：先日报，周报后续
4. ✅ **呈现**：洞察中心是完整查看主战场，报表中心只有缩略索引
5. ✅ **格式**：AI 叙事（顶部）+ 图表（中间）+ 原始数据（底部）
6. ✅ **估时**：约 8.5 天
7. ✅ **风险**：是否接受 fallback 规则引擎、APScheduler 单实例假设

如有调整请告知，确认后进入 Phase 1.1（数据库迁移）。

---

**下一步**：等待您审核本 spec 文档。通过后开始按 Phase 1.1 → 1.2 → 2 → ... 顺序实现。