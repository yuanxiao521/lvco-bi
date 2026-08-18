# LvcoBI 后端分层架构说明

> **创建日期**：2026-08-05
> **版本**：v1.0
> **适用范围**：`backend/app/` 全栈代码

本文档详细说明 LvcoBI 后端采用的分层架构设计、各层职责、依赖关系及最佳实践，便于团队成员理解代码组织、扩展新功能、编写测试。

---

## 一、架构总览

LvcoBI 后端采用经典的四层分层架构，配合依赖注入实现解耦：

```
┌─────────────────────────────────────────────────────────┐
│                  API Route Layer                        │
│        (FastAPI Routers, app/api/v1/)                   │
│  - 参数校验、HTTP 状态码、Swagger 文档                    │
│  - 调用 Service 层执行业务逻辑                           │
│  - 通过 Depends 注入依赖                                 │
└─────────────────────────────────────────────────────────┘
                          ↓ Depends(get_xxx_service)
┌─────────────────────────────────────────────────────────┐
│                  Service Layer                          │
│           (app/services/, 业务逻辑)                     │
│  - 业务规则编排、缓存控制、跨实体协作                     │
│  - 通过 Repository 接口访问数据                          │
│  - 不直接 import SQLAlchemy 进行数据库操作               │
└─────────────────────────────────────────────────────────┘
                          ↓ 构造函数注入
┌─────────────────────────────────────────────────────────┐
│                Repository Layer                         │
│   (app/repositories/protocols.py: Protocol 接口)        │
│  - 定义数据访问的抽象接口                                │
│  - 使用 @runtime_checkable 支持鸭子类型                  │
│  - 不包含业务逻辑                                        │
└─────────────────────────────────────────────────────────┘
                          ↓ 实现
┌─────────────────────────────────────────────────────────┐
│       SQLAlchemy Repository Implementation              │
│   (app/repositories/*_repository.py)                    │
│  - 实现 Protocol 接口，封装 SQLAlchemy ORM 调用          │
│  - 是 SQLAlchemy 唯一被允许直接使用的层                  │
│  - 提供与具体数据库交互的实现                            │
└─────────────────────────────────────────────────────────┘
                          ↓ SQLAlchemy ORM
┌─────────────────────────────────────────────────────────┐
│                  Database (PostgreSQL)                  │
└─────────────────────────────────────────────────────────┘
```

---

## 二、各层职责详解

### 2.1 API Route Layer（路由层）

**位置**：`backend/app/api/v1/`

**核心职责**：
1. 接收 HTTP 请求，解析参数（Path / Query / Body）
2. 通过 Pydantic Schema 验证请求数据
3. 通过 `Depends(get_current_user)` 鉴权并获取当前用户
4. 通过 `Depends(get_xxx_service)` 注入 Service 实例
5. 调用 Service 方法执行业务逻辑
6. 将 Service 返回的结果包装为统一的 `SuccessResponse` / `ErrorResponse`
7. 返回合适的 HTTP 状态码

**关键约束**：
- ❌ 不应在路由层直接 import `AsyncSession` 并执行 SQL
- ❌ 不应在路由层直接调用 SQLAlchemy ORM
- ✅ 应通过 `Depends` 注入所有依赖
- ✅ 应使用 Pydantic Schema 校验输入/输出

**示例**：

```python
# ✅ 推荐写法
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_dashboard(
    body: DashboardCreateBody,
    service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    dashboard = await service.create(
        user_id=current_user.id,
        title=body.title,
        description=body.description,
    )
    return SuccessResponse(data=_summary(dashboard))


# ❌ 反例：直接在路由层操作数据库
@router.post("")
async def create_dashboard(
    body: DashboardCreateBody,
    db: AsyncSession = Depends(get_db),  # ← 反模式
):
    dashboard = Dashboard(user_id=..., title=body.title)  # ← 反模式
    db.add(dashboard)
    await db.flush()
    return dashboard
```

---

### 2.2 Service Layer（服务层）

**位置**：`backend/app/services/`

**核心职责**：
1. 实现核心业务逻辑（业务规则、状态机、跨实体协作）
2. 编排多个 Repository 调用以完成复合操作
3. 控制缓存（读写 cache、清理 cache）
4. 调用外部系统（LLM、文件存储、第三方 API）
5. 抛出业务异常（如 `ValueError`、`HTTPException`）

**关键约束**：
- ❌ 不应在 Service 层直接 `import sqlalchemy` 或 `AsyncSession`
- ❌ 不应在 Service 层直接 `db.execute(select(...))`
- ❌ 不应在 Service 层直接构造 ORM 对象（`Model(...)`）
- ✅ 应通过构造函数注入 Repository 接口（依赖注入）
- ✅ 所有数据库操作都应通过 `self.xxx_repo.xxx_method()` 调用
- ✅ 可以依赖其他 Service（如 `UserPreferenceService` 可被 `AIService` 调用）

**示例**：

```python
# ✅ 推荐写法
class DashboardService:
    def __init__(
        self,
        dashboard_repo: DashboardRepository,
        dashboard_chart_repo: DashboardChartRepository,
        db: AsyncSession,  # 例外：仅用于 QueryEngine 等非数据访问依赖
    ) -> None:
        self.dashboard_repo = dashboard_repo
        self.dashboard_chart_repo = dashboard_chart_repo
        self.db = db

    async def create(self, user_id, title, description):
        return await self.dashboard_repo.create(user_id, title, description)

    async def get_dashboard_data(self, dashboard_id, user_id):
        dashboard = await self.dashboard_repo.get_by_id(dashboard_id, user_id)
        if dashboard is None:
            return None
        # 通过 Repository 查询关联实体
        for dc in dashboard.dashboard_charts:
            chart_config = await self.dashboard_chart_repo.get_chart_config(
                dc.chart_config_id
            )
            # ... 业务逻辑
```

---

### 2.3 Repository Protocol（接口层）

**位置**：`backend/app/repositories/protocols.py`

**核心职责**：
1. 使用 Python `typing.Protocol` 定义数据访问接口
2. `@runtime_checkable` 装饰器允许运行时类型检查
3. 方法签名使用领域语言（如 `get_by_id`、`list_reports`），不暴露 ORM 细节
4. 返回值使用 `Any`（保持解耦），实现层返回 ORM 对象

**已定义的协议**：

| Protocol | 用途 |
|----------|------|
| `CacheRepository` | 缓存读写 |
| `QueryRepository` | 执行查询 |
| `StorageRepository` | 对象存储 |
| `CanvasRepository` | 画布 CRUD |
| `ChartConfigRepository` | 图表配置 |
| `DashboardRepository` | 仪表板 CRUD |
| `DashboardChartRepository` | 仪表板图表关系 |
| `UserPreferenceRepository` | 用户偏好记忆 |
| `ReportRepository` | 报告 CRUD |
| `DataSourceRepository` | 数据源 CRUD |

**示例**：

```python
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
```

---

### 2.4 SQLAlchemy Repository Implementation（实现层）

**位置**：`backend/app/repositories/*_repository.py`

**核心职责**：
1. 实现对应的 Protocol 接口
2. 封装所有 SQLAlchemy ORM 调用（`select`、`add`、`flush`、`refresh`）
3. 处理软删除、默认值、时间戳填充等数据库细节
4. 记录结构化日志（logger.info/debug）

**关键约束**：
- ✅ 唯一允许直接使用 `AsyncSession` 的层
- ✅ 必须实现对应的 Protocol 接口（鸭子类型兼容）
- ❌ 不应包含业务规则（业务规则属于 Service 层）

**示例**：

```python
class SQLAlchemyDashboardRepository:
    """基于 SQLAlchemy 的仪表板仓库实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id: UUID,
        title: str,
        description: str | None = None,
    ) -> Dashboard:
        logger.info(f"创建仪表板 user_id={user_id} title={title}")
        dashboard = Dashboard(
            user_id=user_id,
            title=title,
            description=description,
            layout=[],
        )
        self.db.add(dashboard)
        await self.db.flush()
        await self.db.refresh(dashboard)
        return dashboard
```

---

## 三、依赖注入（Dependency Injection）

**位置**：`backend/app/api/deps.py`

所有依赖（DB Session、Service、Repository）都通过 FastAPI 的 `Depends` 机制注入，避免在 Service / Repository 内部创建依赖。

### 3.1 注入链

```
HTTP Request
    ↓
FastAPI Router
    ↓ Depends(get_db)
    ↓ Depends(get_current_user)
    ↓ Depends(get_dashboard_repo)
    ↓ Depends(get_dashboard_service)
Service (持有 Repository + db)
    ↓
Repository (持有 db)
    ↓
AsyncSession
    ↓
PostgreSQL
```

### 3.2 注入函数规范

| 注入函数 | 返回 | 依赖 |
|----------|------|------|
| `get_db` | `AsyncSession` | 无 |
| `get_current_user` | `User` | `get_db` |
| `get_canvas_repository` | `CanvasRepository` | `get_db` |
| `get_dashboard_service` | `DashboardService` | `get_db` + `get_*_repository` |
| ... | ... | ... |

### 3.3 示例

```python
# app/api/deps.py
def get_dashboard_repository(
    db: AsyncSession = Depends(get_db),
) -> DashboardRepository:
    """获取仪表板仓库实例。"""
    return SQLAlchemyDashboardRepository(db)


def get_dashboard_service(
    dashboard_repo: DashboardRepository = Depends(get_dashboard_repository),
    dashboard_chart_repo: DashboardChartRepository = Depends(get_dashboard_chart_repository),
    db: AsyncSession = Depends(get_db),
) -> DashboardService:
    """获取仪表板服务实例。"""
    return DashboardService(
        dashboard_repo=dashboard_repo,
        dashboard_chart_repo=dashboard_chart_repo,
        db=db,
    )
```

---

## 四、测试策略

### 4.1 测试分层

| 测试类型 | 测试目标 | Mock 对象 | 测试位置 |
|----------|----------|-----------|----------|
| **Service 单元测试** | 业务逻辑 | Mock Repository（AsyncMock） | `tests/test_xxx_service.py` |
| **Repository 单元测试** | SQLAlchemy 交互 | Mock AsyncSession | `tests/test_xxx_repository.py` |
| **集成测试** | 完整链路 | 真实数据库（testcontainers） | `tests/test_integration_*.py` |

### 4.2 测试用例统计

| 模块 | Service 测试 | Repository 测试 |
|------|--------------|------------------|
| Dashboard | 21 个用例 | 19 个用例 |
| Canvas | 待补充 | 待补充 |
| Report | 待补充 | 待补充 |
| DataSource | 待补充 | 待补充 |
| UserPreference | 待补充 | 待补充 |

### 4.3 编写测试的最佳实践

**测试 Service 时**：

```python
@pytest.fixture
def mock_dashboard_repo() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(mock_dashboard_repo: AsyncMock) -> DashboardService:
    return DashboardService(dashboard_repo=mock_dashboard_repo, ...)


async def test_create_calls_repo(service, mock_dashboard_repo):
    mock_dashboard_repo.create.return_value = _make_dashboard_mock()
    await service.create(USER_ID, "标题", "描述")
    mock_dashboard_repo.create.assert_called_once_with(USER_ID, "标题", "描述")
```

**测试 Repository 时**：

```python
@pytest.fixture
def mock_db() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def dashboard_repo(mock_db):
    return SQLAlchemyDashboardRepository(mock_db)


async def test_create_calls_db_methods(dashboard_repo, mock_db):
    with patch("app.repositories.dashboard_repository.Dashboard") as MockDashboard:
        mock_instance = MagicMock()
        MockDashboard.return_value = mock_instance
        await dashboard_repo.create(user_id=USER_ID, title="测试", description="描述")
        MockDashboard.assert_called_once()
        mock_db.add.assert_called_once_with(mock_instance)
```

---

## 五、重构前后对比

### 5.1 重构前（混合层）

```python
class DashboardService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_dashboard_data(self, dashboard_id, user_id):
        # 直接 SQLAlchemy
        dashboard = await self.db.execute(select(Dashboard).where(...))
        for dc in dashboard.dashboard_charts:
            # 又是直接 SQLAlchemy
            cc = await self.db.execute(select(ChartConfig).where(...))
```

**问题**：
- 业务逻辑与数据访问耦合
- 难以单元测试（必须连真实数据库）
- 无法替换 ORM 实现
- 代码异味：Service 中混入了 SQL 拼接

### 5.2 重构后（严格分层）

```python
class DashboardService:
    def __init__(
        self,
        dashboard_repo: DashboardRepository,
        dashboard_chart_repo: DashboardChartRepository,
        db: AsyncSession,
    ) -> None:
        self.dashboard_repo = dashboard_repo
        self.dashboard_chart_repo = dashboard_chart_repo

    async def get_dashboard_data(self, dashboard_id, user_id):
        # 仅调用 Repository
        dashboard = await self.dashboard_repo.get_by_id(dashboard_id, user_id)
        for dc in dashboard.dashboard_charts:
            chart_config = await self.dashboard_chart_repo.get_chart_config(
                dc.chart_config_id
            )
```

**优势**：
- 业务逻辑与数据访问解耦
- 可使用 Mock Repository 单元测试
- 可替换 ORM 实现（如 Tortoise ORM、SQLModel）
- 关注点分离（SoC）清晰

---

## 六、最佳实践

### 6.1 DO（推荐做法）

1. **新增功能时**：
   - 先定义 Protocol 接口
   - 实现 SQLAlchemy Repository
   - 编写 Service 业务逻辑
   - 编写 API Route
   - 编写单元测试

2. **修改现有功能时**：
   - 检查是否违反了分层约束
   - 优先通过 Repository 重构数据访问
   - 保持 Service 与 Repository 的接口稳定

3. **测试时**：
   - Service 测试使用 AsyncMock Repository
   - Repository 测试使用 AsyncMock Session
   - 不要在单元测试中连接真实数据库

### 6.2 DON'T（避免做法）

1. ❌ **不要在 Service 中 import `sqlalchemy`**（除了 `AsyncSession` 类型注解）
2. ❌ **不要在 Service 中调用 `self.db.execute(...)`**
3. ❌ **不要在 Service 中直接构造 ORM 对象**（如 `User(...)`）
4. ❌ **不要在 Repository 中编写业务规则**（如状态机、权限校验）
5. ❌ **不要在路由层处理业务逻辑**（应委托给 Service）

### 6.3 例外情况

某些场景下 Service 可能需要 `AsyncSession`：

1. **QueryEngine**：`execute_chart_query()` 接收 `db` 参数执行查询
2. **跨事务操作**：需要在一个事务中完成多个 Service 调用
3. **复杂查询**：需要拼接 SQL 但封装在 Repository 不划算

遇到这些场景时，应在代码注释中明确说明 `db` 的用途。

---

## 七、迁移指南

如果未来需要将整个数据访问层从 SQLAlchemy 切换到其他 ORM：

1. **实现新的 Repository**（如 `TortoiseDashboardRepository`）
2. **修改 `app/api/deps.py`** 中的 `get_*_repository` 函数返回新实现
3. **Service 层无需任何改动**（这是分层架构的最大价值）
4. **测试无需修改**（Protocol 接口稳定）

---

## 八、参考资源

- [The Repository Pattern (martinfowler.com)](https://martinfowler.com/eaaCatalog/repository.html)
- [Python typing.Protocol (PEP 544)](https://peps.python.org/pep-0544/)
- [Clean Architecture (Robert C. Martin)](https://blog.cleancoder.com/uncle-bob/2012/08/13/clean-architecture.html)
- [FastAPI Dependency Injection](https://fastapi.tiangolo.com/tutorial/dependencies/)

---

## 九、版本历史

| 版本 | 日期 | 变更说明 |
|------|------|----------|
| v1.0 | 2026-08-05 | 初版，描述 Service → Repository → Core 分层架构 |