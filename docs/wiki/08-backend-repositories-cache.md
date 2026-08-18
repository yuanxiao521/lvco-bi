# 08 · 仓储层与缓存

> 目录：`app/repositories/`。仓储模式 + 三层缓存，配合 `api/deps.py` 的依赖注入工厂。

## 模式设计

```
Protocol 接口层（protocols.py，typing.Protocol，@runtime_checkable）
      ↑ 依赖倒置（业务层只依赖接口，支持 mock/鸭子类型）
SQLAlchemy 实现层（各 *_repository.py，接收 AsyncSession）
      ↑ 约定：方法只 flush/refresh，永不 commit（由 UoW/Service 控制事务）
UnitOfWork（unit_of_work.py，统一事务边界）
```

### 接口清单（protocols.py，10 个）

`CacheRepository`（get/set/delete/exists）· `QueryRepository` · `StorageRepository` · `CanvasRepository` · `ChartConfigRepository` · `DashboardRepository` · `DashboardChartRepository` · `UserPreferenceRepository` · `ReportRepository` · `DataSourceRepository`

### UnitOfWork

- `__init__` 时把 **12 个 Repository** 绑定到同一个 `db` 会话。
- `commit()` 为唯一提交点，失败自动 rollback；`__aexit__` 遇异常自动 rollback；`flush()` 强制刷出。

### 实现类清单

| 文件 | 类 | 职责 |
|------|-----|------|
| `user_repository.py` | SQLAlchemyUserRepository | 用户 CRUD（create/update 用 returning） |
| `datasource_repository.py` | SQLAlchemyDataSourceRepository | 数据源 CRUD + 分页筛选 |
| `datasource_schema_repository.py` | SQLAlchemyDatasourceSchemaRepository | schema 只读（供 DataQualityService，解析 DuckDB schema/表名） |
| `canvas_repository.py` | SQLAlchemyCanvasRepository + SQLAlchemyChartConfigRepository | 画布 CRUD + 图表配置创建 |
| `dashboard_repository.py` | SQLAlchemyDashboardRepository + SQLAlchemyDashboardChartRepository | 仪表盘 CRUD（软删/分享）+ 图表管理 |
| `report_repository.py` | SQLAlchemyReportRepository | 报表 CRUD（状态归一化/软删过滤/分享） |
| `notification_repository.py` | SQLAlchemyNotificationRepository | 通知创建/分页/未读数/已读/批量创建 |
| `ai_session_repository.py` | SQLAlchemyAISessionRepository + SQLAlchemyAIMessageRepository | 会话与消息（共享 session） |
| `user_preference_repository.py` | SQLAlchemyUserPreferenceRepository | 偏好查询/创建/更新/删除，按 strength 排序取 Top N |

## 三层缓存策略

```
┌─────────────────────────────────────────────┐
│ FallbackCacheRepository（默认 get_cache_repository）│
│   优先 Redis，不可用降级内存；set/delete 双写   │
├──────────────────────┬──────────────────────┤
│ RedisCacheRepository │ InMemoryCacheRepository│
│ key 前缀 lvco:       │ dict + expire_at      │
│ 连接失败静默降级 None │ 用于测试/兜底           │
└──────────────────────┴──────────────────────┘
```

- **InMemoryCacheRepository**：dict + 过期时间戳，读/查时惰性清理过期条目，`clear()/keys()` 供测试。
- **RedisCacheRepository**：key 统一前缀 `lvco:`；连接 3 秒超时，失败记 warning 后所有方法静默返回 None（不抛异常）；TTL 默认 `settings.redis_ttl`（300s）。
- **FallbackCacheRepository**：构造时探测 Redis 可用性；`get/exists` 优先 Redis（miss 再查内存），`set/delete` 双写两个后端，保证降级路径数据一致。
- **测试环境**：`deps.py` 的 `get_test_cache_repository()` 返回纯内存实现。
- 另有 `cache_service.py` 的 `CacheService`（单例 `cache`）：`SimpleCache` 兜底 + Redis 的同构封装，与 repositories 缓存并存两套实现。

## 依赖注入装配（`api/deps.py`）

```python
get_current_user(credentials, db) -> User          # HTTPBearer → JWT(type=access) → 查库，401 中文错误码
get_cache_repository()                             # FallbackCacheRepository 单例
get_test_cache_repository()                        # 内存实现
get_canvas_repository / get_chart_config_repository / get_dashboard_repository /
get_dashboard_chart_repository / get_report_repository / get_datasource_repository /
get_user_preference_repository / get_user_repository / get_notification_repository /
get_ai_session_repository / get_ai_message_repository / get_datasource_schema_repository
get_canvas_service / get_dashboard_service / get_report_service / get_datasource_service /
get_user_preference_service / get_auth_service / get_notification_service / get_data_quality_service
```

> 设计要点：`deps.py` 是全应用的唯一装配点，Repository/Service 均通过工厂函数注入，便于测试替换（mock Protocol）。
