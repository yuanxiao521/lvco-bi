# 09 · 数据源连接器、数据库迁移与脚本

## 连接器（`app/connectors/`）

### 抽象基类 `base.py`

```python
class BaseDataSourceConnector(ABC):
    def load_to_duckdb(client, file_path, schema_name) -> str   # 返回表名
    def get_schema_meta(client, schema_name, table_name) -> dict  # {"fields":[...]}
    def get_row_count(client, schema_name, table_name) -> int
    # PG/MySQL 额外实现（非抽象）：test_connection / get_attach_sql / list_tables / list_tables_direct
```

### 各实现

| 连接器 | 核心机制 |
|--------|---------|
| `csv_connector.py` | `read_csv_auto` 直接建表，字段分类推断 |
| `excel_connector.py` | openpyxl 读全量 → 写临时 CSV → `read_csv_auto` 建表 |
| `mysql_connector.py` | 测试连接 + 生成 `ATTACH 'mysql://user:urlencode(pw)@host:port/db' AS "schema" (TYPE mysql)`；schema 提取（单例 `mysql_connector`） |
| `postgres_connector.py` | psycopg2 直连测试/列表；`ATTACH 'host=... user=... password=... dbname=...' AS "schema" (TYPE postgres, READ_ONLY)`；schema 提取（单例 `postgres_connector`） |

### 数据源注册两条路径（`datasource_service.py`）

**A. 文件型（CSV/Excel）`POST /datasources/upload`**
1. 按扩展名实例化连接器（每次 new）；
2. 建 DataSource 记录（status=disconnected）；
3. 同步时：`duckdb_client.get_schema_name()` 生成隔离 schema → `load_to_duckdb` 灌入 `"schema"."data"` → 取 row_count / schema_meta 写入。

**B. 数据库型（PG/MySQL）`POST /datasources/connect`**
1. 解密 connection_config 密码（`utils/crypto.decrypt_value`），键名映射（username→user、db_name→database）；
2. `DETACH` 旧连接后执行 `get_attach_sql` ATTACH（PG READ_ONLY / MySQL）；
3. `list_tables` 列 public 表（过滤 `pg_/sql_` 系统表）；
4. 取字段（含 category 分类 measure/time/key/dimension + 3 个抽样值 sample）、行数（`row_count*200` 粗估 size_bytes）；
5. 状态置 connected。

> schema 隔离命名：`get_schema_name()` 优先级 `db_name > datasource_name > user_hash`，均拼 `<name>_<数据源ID前8位>`，避免跨库串数据。

### DuckDB 扩展

- `spatial`：Excel 读取；`postgres_scanner`：PostgreSQL ATTACH / 洞察引擎使用。

## 数据库迁移（`alembic/versions/`，链式 0001→0013）

```
0001_init                           # 初始建库：7 个 PG ENUM + 8 张表
0002_add_manual_source_type         # 空操作占位
0003_extend_chart_type              # chart_type += funnel/heatmap/radar/sankey
0004_add_favorites_table            # favorites + 枚举
0005_add_soft_delete                # canvases/dashboards.deleted_at; report_status += deleted
0006_drop_favorites_table           # 下线收藏夹
0007_add_render_config              # chart_configs.render_config JSONB
0008_extend_chart_type_phase4       # += grouped_bar/stacked_bar/kpi_card
0009_add_insight_tables             # insight_rules/records/suggestions + notifications
0010_add_horizontal_bar             # += horizontal_bar（文件名为 0009_add_horizontal_bar.py，内部 revision 为 0010）
0011_add_operation_log              # operation_logs + 3 索引
0012_add_ai_insight_report_source   # report_source_type += ai_insight
0013_add_user_preferences           # user_preferences + 3 索引
```

**执行**：`alembic upgrade head`（`alembic/env.py` 用 `settings.DATABASE_URL` 覆盖 alembic.ini，异步引擎执行）。

**注意事项**：
- 枚举扩展迁移用 `op.get_context().autocommit_block()`（ALTER TYPE ... ADD VALUE 不能在事务内执行）；downgrade 多为空操作。
- `0009_add_horizontal_bar.py` 内部 revision 为 `0010_add_horizontal_bar`，与 `0009_add_insight_tables` 同名前缀易混淆。
- 0013 建表未加 FK 约束（与 ORM 模型不一致，历史遗留）。
- 迁移不含 `uuid_generate_v4()` 的 PG 扩展创建逻辑依赖，若未预装 `uuid-ossp` 需注意（可改用 gen_random_uuid）。

## 脚本工具（`scripts/`）

| 脚本 | 用途 |
|------|------|
| `generate_mock_data.py` | `random.seed(42)` 生成 6 个模拟 CSV 到 `mock_data/`（电商订单 1200 行/财务月度 72/产品绩效 300/客户指标 600/营销活动 250/员工销售 200） |
| `upload_mock_data.py` | 探测 /health → 注册/登录 `test@example.com`/`test123456` → 逐个 POST 上传为数据源 |
| `quick_check.py` | 登录后列出数据源状态/字段数/行数/错误 |
| `pdf_worker.py` | Playwright 无头子进程把 HTML 渲染为 A4 PDF（规避 Windows ProactorEventLoop） |
| `setup_pg_datasource.py` | psycopg2 在 PG 建 6 张测试表并灌数据 |
| `register_pg_datasources.py` / `register_pg_8001.py` | 把 PG 表注册为数据源并 sync（8000/8001 端口） |
| `test_duckdb_attach.py` | 验证 DuckDB ATTACH PostgreSQL 链路 |
