# 数据源连接层：把"能连"做成"能连、连得住、连得安全"

> 本文记录 2026-11 数据源连接改造。目标不是加功能，而是把"接数据库"这条链路上的
> **安全债**和**可用性债**一起还掉：改造前是 demo 级实现（密码明文落库、明文回传前端、
> 密码拼进 SQL、干净环境装完依赖连不上 PG），改造后是一条有明确边界的连接层。

## 1. 改造前的实测问题（真机取证）

| # | 现象 | 取证方式 | 危害 |
|---|------|----------|------|
| 1 | 数据库密码**明文落库** | `SELECT connection_config FROM datasources` → 明文 password | 一次库备份泄露 = 客户库凭据泄露 |
| 2 | 密码**明文回传前端** | connect / get 响应体直接带 password | devtools、代理、前端日志都能拿到 |
| 3 | 密码被拼进 DuckDB ATTACH 连接串 | `SELECT path FROM duckdb_databases()` 读到 `password=lvco_secret` | 凭据进入引擎 catalog 元数据，任何 SQL 通道都可能读到 |
| 4 | 连接参数无校验 | host 传 169.254.169.254、`127.0.0.1;DROP`、带空格引号的库名 | SSRF（云元数据/内网扫端口）+ 语法注入面 |
| 5 | PG 数据源在干净环境**连不上** | 同步报 `Required module pytz failed to import` | 文档说支持 PG，实际不可用（requirements 漏装 pytz） |
| 6 | 带 UUID/时间列的表同步 500 | `Object of type UUID is not JSON serializable` | 真实业务表几乎都有 UUID 主键 → PG 数据源等于不可用 |
| 7 | 数据库型数据源**预览恒为空** | preview 对非 CSV 源直接返回空数组 | "连上了但看不到数据"，典型 demo 体验 |
| 8 | 同一段连接逻辑在 **10 处**各写一遍 | datasource_service ×2、query_engine、agent_tools、statistics、ai.py ×3、insight_engine ×2 | 行为漂移（有的吞异常、mysql/pg 默认用户不一致），修一处漏九处 |
| 9 | requirements 缺 7 个运行期依赖 | 对比 app/ 的 import 与 requirements.txt | 换机器 pip install -r 后起不来/连不上 |

## 2. 威胁模型

谁可能拿到凭据：

1. **存储介质**（PG 备份、慢查询日志、DBA 账号）——落库必须密文；
2. **前端 / 链路**（devtools、代理、埋点）——API 不回传密码；
3. **分析引擎内部**（DuckDB catalog、错误串、应用日志）——密码不进 ATTACH 连接串，错误统一脱敏；
4. **输入侧攻击者**（把"连数据库"当 SSRF 跳板）——连接参数与网络边界要校验；
5. **模型侧**（LLM 生成的 SQL / 提示注入）——外部分析源一律 READ_ONLY 附加，密码进 Secret 而非连接串。

## 3. 分层设计

```
HTTP 层  app/api/v1/datasources.py
  ├─ 入参校验（connection_guard.validate_connection_input）
  ├─ 限流（/connect、/test-connection 各 20/min）
  └─ 响应脱敏（DataSourceResponse 的 field_validator 剔除 password）
        │
服务层  app/services/datasource_service.py
  ├─ 落库前加密（seal_connection_config → crypto.seal_secret）
  └─ 同步时把历史明文/旧密文顺手升级为 v2
        │
连接层  app/services/datasource_connection.py（唯一入口）
  ├─ canonicalize()   兼容 host/port/db_name/database、username/user 等历史命名
  ├─ resolve_credentials()  解密（只在内存）
  ├─ ensure_attached()  附加失败转成已脱敏的 DatasourceConnectionError
  └─ sanitize_error()   抹掉 password=xxx、user:pwd@host
        │
引擎层  app/connectors/attach_sql.py
  ├─ CREATE OR REPLACE SECRET <name> (TYPE postgres|mysql, HOST, PORT, USER, PASSWORD, DBNAME)
  └─ ATTACH '' AS "schema" (TYPE ..., SECRET <name>, READ_ONLY)   ← 密码不在这里
        │
存储层  app/utils/crypto.py
  └─ AES-256-GCM 版本化信封：enc:v2:<b64(nonce12 + ciphertext + tag16)>
```

### 3.1 为什么密码要从 ATTACH 连接串搬进 SECRET

改造前后在同一个 DuckDB 实例上对比（真机）：

| | 旧：连接串带密码 | 新：SECRET + ATTACH 空串 |
|---|---|---|
| duckdb_databases().path | `host=127.0.0.1 port=5432 user=lvco password=lvco_secret dbname=lvco_bi` | 空串 |
| duckdb_secrets() | 无 | `...;password=redacted;...` |
| 密码是否出现在 ATTACH 报错里 | 取决于驱动回显 | 不会 |

旧写法把凭据写进了**引擎元数据**，新写法把凭据收进**引擎的密钥管理**，且默认对查询侧脱敏。
这是防御纵深里最实在的一层：即便将来某条 SQL 通道被绕过，也读不到密码。

### 3.2 为什么用 AES-GCM 而不是原来的 CBC

原实现是 AES-256-CBC + 随机 IV + base64，没有完整性校验：错误密钥解出来的是"看似合法"的垃圾，
症状表现为"密码错误"而不是"密钥不对"，排查会被带偏。GCM 带认证标签，密钥不匹配/密文被篡改
会明确报 CredentialDecryptError。

密文带版本前缀，读路径同时兼容三种历史形态（不动存量数据也能跑）：

| 形态 | 处理 |
|---|---|
| `enc:v2:...` | 当前格式 |
| 无前缀 base64（长度满足 IV+16n） | 旧 CBC，可解，下次写入升级为 v2 |
| 其他（明文） | 原样返回（不炸旧数据源），同步时自动升级为 v2 |

一次性迁移脚本：`backend/scripts/migrate_datasource_secrets.py --apply`（默认 dry-run）。

### 3.3 fail-closed：没有密钥就不许连库

`DB_ENCRYPTION_KEY` 缺失时：创建/修改数据库型数据源 → `503 ENCRYPTION_KEY_MISSING` 并给出生成命令；
绝不"降级为明文存一下"。宁可功能不可用，也不留一条"默默明文存密码"的路径——
这种路径一旦存在，测试环境、演示环境、客户 POC 环境里就一定有人踩。

### 3.4 网络边界（SSRF）

BI 产品天然要连内网库，所以策略不是"禁内网"，而是：

- **始终禁止**：169.254.0.0/16（AWS/GCP/Azure 元数据）、100.100.100.200（阿里云元数据）、
  fd00:ec2::254、0.0.0.0、255.255.255.255、组播、保留地址；
- **默认允许**：私网/回环（`DB_ALLOW_PRIVATE_HOSTS=true`）；
- **可选收口**：`DB_ALLOWED_HOSTS=host1,corp-db.example.com`（命中或子域才算通过）；
- **限流**：`/test-connection`、`/connect` 各 20/min，避免被当内网端口扫描器。

## 4. 顺带修掉的可用性债

| 问题 | 修法 |
|---|---|
| 缺 pytz 导致 PG ATTACH 失败 | requirements 补 pytz/tzdata；缺驱动时返回可读提示 |
| UUID/时间/bytea 样例值不可序列化 | 抽出 app/utils/json_safe.py（date/time/timedelta/Decimal/UUID/Enum/bytes 递归处理） |
| 单列取样例失败就整表失败 | 每个字段独立 try/except，只丢该列 sample |
| DB 数据源预览恒为空 | `_preview_external()`：ATTACH 后 SELECT * LIMIT n，沿用 schema_meta 列名 |
| MySQL 表引用被拼成 .public."t" | 统一走 attach_sql.table_ref()（PG 三段式 / MySQL 两段式） |
| 错误串直接进 schema_meta 给用户看 | 统一 sanitize_error()；连接类错误包成中文可读消息 |

## 5. 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| DB_ENCRYPTION_KEY | 无（必填） | 64 位 hex；`python -c "import secrets;print(secrets.token_hex(32))"` |
| DB_ALLOW_PRIVATE_HOSTS | true | 是否允许私网/回环目标 |
| DB_ALLOWED_HOSTS | 空 | 逗号分隔白名单；非空即收口 |
| DB_CONNECT_TIMEOUT_SECONDS | 10 | 建连超时 |

## 6. 验证方式

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests/test_datasource_security.py -q   # 32 条安全用例
.venv/Scripts/python.exe scripts/migrate_datasource_secrets.py           # dry-run 存量清查
```

端到端（真机已跑通）：

1. test-connection 传 169.254.169.254 → 400 INVALID_CONNECTION；
2. 传 `127.0.0.1;DROP` → 400（非法字符）；
3. 正常 PG 连接 → 200 success=true；
4. 特殊字符密码 `p'a ss;--x` 建源 → 201，响应 connectionConfig 只剩 host/port/db_name/username/table_name/secret_format/has_password；
5. 库内 connection_config.password = enc:v2:...；
6. /sync → connected，UUID 列样例正常入库；
7. /preview → 返回真实数据行（改造前恒为空）。

## 7. 还没做完（诚实清单）

- **TLS 未强制**：PG 支持透传 sslmode 但默认不要求；MySQL 只做了 ssl_disabled 开关，证书校验（ssl_ca）没做。
  生产应默认 sslmode=require 并提供 CA 配置。
- **敏感列治理**：连业务库时 password_hash 这类列会出现在 schema 样例/preview 里，进而可能进入 LLM 上下文。
  需要"列级排除/脱敏"的用户配置（下一步）。
- **一表一数据源**：前端连接多表时按表建多个数据源，凭据在库里存多份副本。应改为"一个连接 + 多表"。
- **洞察引擎两处旧形态**：insight_engine/runner.py、auto_discovery.py 仍在模块内自己解密
  （单测 patch 了模块属性）。它们产出的 ATTACH 语句已是安全形态，但调用形态未收敛到唯一入口。
- **凭据轮换/失效**：没有 UI，只有重新连接；ensure_attached(force=True) 支持重建。
- **连接健康度**：没有连接池与健康检查指标（"这个数据源现在还能连吗"只能靠用户点同步）。

---

# 第二部分（2026-11 追加）：连接形态、TLS、敏感列、轮换与健康

第一部分解决“凭据不被泄漏”。这一部分解决**产品可用性**：能不能连得上（TLS）、
连上后会不会把隐私数据喂给模型（敏感列）、改密码要不要改 N 次（一份凭据多张表）、
数据源挂了能不能提前发现（健康检查）。

## 8. 连接模型：一份凭据 → 多张表

改造前：连一张表 = 一个 datasource 行 = 一份密码密文。选 3 张表就有 3 份副本，
改密码要改 3 次、无原子性，健康检查也要探测 3 次。

现在：

```
db_connections (id, user_id, source_type, host, port, db_name, username,
                password_enc, ssl_mode, ssl_ca_enc, status,
                health_status, health_error, last_health_at)
      ▲ connection_id
      │
datasources (…, connection_id, connection_config = {table_name, db_name, host, port, username})
```

- 连接向导一次提交 tableNames=[orders, customers] → 后端建 1 个连接 + N 个数据源；
- 同用户相同 (host, port, db, user) 自动复用连接（dedup），不会产生多份凭据；
- datasource.connection_config 里不再有 password，只留非敏感元数据（兼容旧读取路径）；
- 删除最后一张表时自动回收连接并 DETACH 对应 catalog。

迁移：0021_add_db_connections（按物理连接去重生成连接行、回填 connection_id、剥离密码字段）。
已验证：迁移前插入历史形态数据源 → 迁移后连接行生成、datasource 关联、password 从 config 消失。

## 9. TLS：默认优先加密，可按需强制

db_connections.ssl_mode 取值对齐 libpq：disable / prefer / require / verify-ca / verify-full；
ssl_ca_enc 存 CA（PEM，与密码同一套 AES-256-GCM 加密）。贯通三层：

| 层 | PG | MySQL |
|---|---|---|
| 驱动探测 | sslmode + sslrootcert | ssl_disabled / ssl_ca / ssl_verify_cert / ssl_verify_identity |
| DuckDB SECRET | SSLMODE + SSLROOTCERT | SSL_MODE(disabled/preferred/required/verify_ca/verify_identity) |
| 落盘 | CA 由 materialize_ca() 写到 data/tls/ca_<conn>.pem（0600） | 同 |

已验证：把连接从 prefer 改成 require 后，本机 PG（未开 SSL）所有表立即重建为 disconnected
并带可读错误；改回 prefer 后恢复 connected。

## 10. 敏感列治理（列级）

风险：schema 同步取 3 个样例、预览返回前 N 行，这两条数据都会进 schema_meta 和 LLM 上下文；
真实库里的 password_hash / id_card / bank_card 一旦进模型，等于把 PII 发给第三方。

策略（app/services/sensitive_columns.py）：

1. 识别：列名启发式（password/pwd/secret/token/api_key/private_key/credential/cvv/id_card/
   bank_card/ssn/phone/身份证/银行卡/手机号…），命中写 fields[i].sensitive + sensitive_reason；
2. 不出样例：敏感列同步时 sample = []；
3. 预览打码：mask_rows() 把敏感列值替换成 ***（文件型与数据库型预览同一函数）；
4. 工具层：list_fields 把敏感列移到 sensitive_columns 并附 sensitive_hint，不给样例；
5. 查询层：query_sql 执行前用 referenced_sensitive() 拦 SQL；query_engine 拦图表配置的维度/度量/筛选/排序；
6. 可覆盖：前端字段编辑器的“敏感”开关写回 schemaMeta.fields[i].sensitive，手工优先。

实测：users 表同步后 password_hash 被标记且样例为空；/preview 返回 ***。

## 11. 轮换与健康

轮换密码 POST /connections/{id}/rotate-secret：

1. 用新密码先探测（不写库）；
2. 失败 → 400 + 可读原因（旧密文保持不变，绝不覆盖成不可用凭据）；
3. 成功 → 加密落库 → 重建该连接下所有表 ATTACH（force）→ 逐表更新状态（失败的表标 disconnected，不假绿）。

健康检查（连接级）：

- 手动：POST /connections/{id}/health、GET /datasources/{id}/health（5 分钟内复用结果）；
- 自动：ConnectionHealthScheduler 每 15 分钟探测“最久没检查/从未检查”的连接（单轮上限 20），
  写 health_status / health_error / last_health_at；
- 前端：列表状态列下的健康徽章 + 行内“检查”按钮；详情弹窗展示连接信息与最近检查时间。

## 12. 新增/变更 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /datasources/connect | 扩展 tableNames[] / sslMode / sslCaPem；响应 {connection, datasources[]} |
| GET | /connections | 连接列表（含健康状态、表数量） |
| GET/PATCH | /connections/{id} | 查看 / 改名称、TLS |
| POST | /connections/{id}/rotate-secret | 轮换密码（先验证后落库） |
| POST | /connections/{id}/health | 手动健康探测 |
| GET | /datasources/{id}/health | 数据源视角的健康探测 |

## 13. 配置项（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| TLS_CA_DIR | ./data/tls | CA 落地目录（DuckDB 需要文件路径） |
| CONNECTION_HEALTH_ENABLED | true | 是否启用定时健康检查 |
| CONNECTION_HEALTH_INTERVAL_MINUTES | 15 | 探测周期 |
| CONNECTION_HEALTH_MAX_PER_CYCLE | 20 | 单轮最多探测连接数 |
| CONNECTION_HEALTH_MIN_AGE_MINUTES | 5 | 多久内探测过就跳过 |

## 14. 验证与迁移命令

```bash
cd backend
.venv/Scripts/python.exe -m alembic upgrade head                        # 0021 连接表
.venv/Scripts/python.exe scripts/migrate_datasource_secrets.py --apply  # 历史明文/CBC → v2（含连接表）
.venv/Scripts/python.exe -m pytest tests/test_connections_and_governance.py -q  # 19 条新用例
```

全量回归：1119 passed。前端：npx tsc --noEmit 与 npm run build 通过；Playwright 冒烟
（登录 → 源数据管理 → 新建数据源 → 数据库直连 → TLS 高级选项）0 console error。

## 15. 仍未做

- 一连接多库：目前一个连接对应一个 database；跨库查询仍要建多个连接。
- 权限自检：连上后没有校验“这个账号是否只读”，只做了 READ_ONLY 附加（引擎层只读）。
- 敏感列只按列名启发式，没做值级识别（身份证/手机号格式校验），也没有行级脱敏/审批流。
- 健康检查不写时序指标：只有状态字段，想做“连接可用率”需要接指标系统。
- CA/证书轮换：可以 PATCH 换 CA，但没有到期提醒。
