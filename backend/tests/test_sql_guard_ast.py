"""基于 AST 的 SQL 安全校验层（L4）单元测试。

覆盖范围（P3-A 放宽后）：
- parse_sql 解析
- check_statement_type 语句类型校验（含 UNION/PIVOT 放行）
- check_cross_join：只拦隐式笛卡尔，显式 CROSS JOIN / 标量广播放行
- SELECT * / 子查询深度 / 聚合白名单：已放行，只留 observable 标记
- check_dangerous_functions 危险函数黑名单（含 DuckDB 文件表函数家族）
- check_table_refs 表引用格式校验（表函数跳过、文件路径拒绝）
- inject_limit 自动 LIMIT 注入
- ast_full_check 全量校验入口（含裸表名自动补全 / 表归属）
- 敏感列旁路（agent_tools AST 版）
- 边界与异常情况

运行：
    cd backend
    pytest tests/test_sql_guard_ast.py -v
"""
from __future__ import annotations

import pytest

from app.services.sql_guard_ast import (
    DANGEROUS_FUNCTIONS,
    parse_sql,
    check_statement_type,
    check_cross_join,
    check_dangerous_functions,
    check_table_refs,
    check_table_ownership,
    check_sql_table_ownership,
    inject_limit,
    ast_full_check,
)


# ==================== 辅助函数 ====================


def _parse_one(sql: str):
    """解析单条 SQL 并返回 AST 根节点，方便测试各 check 函数。"""
    parsed = parse_sql(sql)
    assert parsed is not None, f"SQL 解析失败: {sql}"
    return parsed[0]


# ==================== 1. parse_sql 解析测试 ====================


class TestParseSql:
    """parse_sql：SQL 解析为 AST 节点列表。"""

    def test_正常_sql_解析成功(self):
        """普通 SELECT 语句应解析为非空列表。"""
        parsed = parse_sql("SELECT 1")
        assert parsed is not None
        assert len(parsed) >= 1
        assert parsed[0] is not None

    def test_with_cte_解析成功(self):
        """WITH ... SELECT 语句应解析成功。"""
        parsed = parse_sql("WITH x AS (SELECT 1) SELECT * FROM x")
        assert parsed is not None
        assert len(parsed) >= 1

    def test_复杂查询_多表join_解析成功(self):
        """带 JOIN 的复杂查询应解析成功。"""
        parsed = parse_sql(
            "SELECT a.id, b.name FROM schema.orders a "
            "INNER JOIN schema.customers b ON a.cid = b.id "
            "WHERE a.amount > 100 GROUP BY b.name"
        )
        assert parsed is not None
        assert len(parsed) >= 1

    def test_空字符串返回_none(self):
        """空字符串应返回 None。"""
        assert parse_sql("") is None

    def test_仅空白字符返回_none(self):
        """仅含空白字符的 SQL 应返回 None。"""
        assert parse_sql("   ") is None
        assert parse_sql("\n\t  \n") is None

    def test_仅含注释返回_none(self):
        """仅含注释的 SQL 应返回 None（注释被剥离后为空）。"""
        assert parse_sql("-- 这是一条注释") is None
        assert parse_sql("/* 多行注释 */") is None

    def test_畸形_sql_返回_none(self):
        """语法错误的 SQL 应返回 None。"""
        assert parse_sql("SELECT FROM WHERE") is None
        # 完全无意义的字符串应解析失败
        assert parse_sql("??? !!! @@@") is None

    def test_不完整_sql_返回_none(self):
        """不完整的 SQL 片段应返回 None。"""
        result = parse_sql("SELECT")
        # SQLGlot 可能对此容忍，我们只确保不崩溃
        # 如果返回了，则结果不应为 None（但可能解析出一个残缺的 Select 节点）
        # 这里不做强制断言，仅确保不抛异常
        assert result is None or len(result) >= 1

    def test_unicode_sql_不崩溃(self):
        """含 Unicode 标识符的 SQL 不应崩溃。"""
        parsed = parse_sql("SELECT 用户.姓名 FROM schema.用户表")
        # 可能解析失败，但不应抛异常
        if parsed is not None:
            assert len(parsed) >= 1


# ==================== 2. check_statement_type 语句类型校验 ====================


class TestCheckStatementType:
    """check_statement_type：仅允许 SELECT / WITH...SELECT。"""

    def test_select_通过(self):
        """SELECT 语句应通过校验。"""
        ast = _parse_one("SELECT 1")
        passed, reason = check_statement_type(ast)
        assert passed is True
        assert reason == ""

    def test_select_多列_通过(self):
        """SELECT 多列的语句应通过。"""
        ast = _parse_one("SELECT a, b, c FROM schema.t")
        passed, reason = check_statement_type(ast)
        assert passed is True
        assert reason == ""

    def test_with_select_通过(self):
        """WITH ... SELECT 语句应通过校验。"""
        ast = _parse_one("WITH x AS (SELECT 1) SELECT * FROM x")
        passed, reason = check_statement_type(ast)
        assert passed is True
        assert reason == ""

    def test_with_多层cte_通过(self):
        """多层 CTE 的 WITH ... SELECT 应通过。"""
        ast = _parse_one(
            "WITH x AS (SELECT 1 AS a), y AS (SELECT 2 AS b) "
            "SELECT x.a, y.b FROM x, y"
        )
        passed, reason = check_statement_type(ast)
        assert passed is True
        assert reason == ""

    def test_insert_被拦截(self):
        """INSERT 语句应被拦截。"""
        ast = _parse_one("INSERT INTO schema.t VALUES (1)")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # reason 包含类名 "Insert"
        assert "Insert" in reason

    def test_drop_被拦截(self):
        """DROP 语句应被拦截。"""
        ast = _parse_one("DROP TABLE schema.t")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # reason 包含类名 "Drop"
        assert "Drop" in reason

    def test_create_被拦截(self):
        """CREATE 语句应被拦截。"""
        ast = _parse_one("CREATE TABLE schema.t (id INT)")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # reason 包含类名 "Create"
        assert "Create" in reason

    def test_alter_被拦截(self):
        """ALTER 语句应被拦截。"""
        ast = _parse_one("ALTER TABLE schema.t ADD COLUMN x INT")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # reason 包含类名 "Alter"
        assert "Alter" in reason

    def test_delete_被拦截(self):
        """DELETE 语句应被拦截。"""
        ast = _parse_one("DELETE FROM schema.t WHERE id = 1")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # reason 包含类名 "Delete"
        assert "Delete" in reason

    def test_update_被拦截(self):
        """UPDATE 语句应被拦截。"""
        ast = _parse_one("UPDATE schema.t SET name = 'x'")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # reason 包含类名 "Update"
        assert "Update" in reason

    def test_describe_被拦截(self):
        """DESCRIBE 语句应被拦截。"""
        ast = _parse_one("DESCRIBE schema.t")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # DESCRIBE 可能被解析为其他类型，只要被拦截即可
        assert "DESCRIBE" in reason or "Describe" in reason or "仅允许" in reason

    def test_show_被拦截(self):
        """SHOW 语句应被拦截。"""
        ast = _parse_one("SHOW TABLES")
        passed, reason = check_statement_type(ast)
        assert passed is False
        # SHOW 可能被解析为其他类型，只要被拦截即可
        assert "SHOW" in reason or "Show" in reason or "仅允许" in reason

    def test_explain_select_被拦截(self):
        """EXPLAIN SELECT 应被拦截（EXPLAIN 不是 SELECT）。"""
        ast = _parse_one("EXPLAIN SELECT 1")
        passed, reason = check_statement_type(ast)
        assert passed is False
        assert "仅允许" in reason


# ==================== 3. 集合运算 / PIVOT（放行：分支必须是查询）====================


class TestSetOperationsRelaxed:
    """UNION / EXCEPT / INTERSECT 不再拦截：表归属校验遍历所有分支，集合操作不构成提权。"""

    def test_union_all_通过(self):
        ast = _parse_one("SELECT id FROM schema.a UNION ALL SELECT id FROM schema.b")
        passed, reason = check_statement_type(ast)
        assert passed is True
        assert reason == ""

    def test_union_distinct_通过(self):
        ast = _parse_one("SELECT id FROM schema.a UNION SELECT id FROM schema.b")
        passed, reason = check_statement_type(ast)
        assert passed is True

    def test_except_通过(self):
        ast = _parse_one("SELECT id FROM schema.a EXCEPT SELECT id FROM schema.b")
        passed, reason = check_statement_type(ast)
        assert passed is True

    def test_intersect_通过(self):
        ast = _parse_one("SELECT id FROM schema.a INTERSECT SELECT id FROM schema.b")
        passed, reason = check_statement_type(ast)
        assert passed is True

    def test_pivot_通过(self):
        ast = _parse_one("PIVOT schema.t ON a USING SUM(b)")
        passed, reason = check_statement_type(ast)
        assert passed is True

    def test_集合运算_分支含写语句_无法构造但仍由语句类型兜底(self):
        """DML 不能作为集合运算分支（解析层即失败），这里确认解析失败不入执行。"""
        assert parse_sql("SELECT 1 UNION INSERT INTO x VALUES (1)") is None


# ==================== 5. check_cross_join CROSS JOIN 检测 ====================


class TestCheckCrossJoin:
    """check_cross_join：只拦"隐式笛卡尔"，显式 CROSS JOIN / 标量广播放行。"""

    def test_inner_join_通过(self):
        ast = _parse_one(
            "SELECT a.id, b.name "
            "FROM schema.orders a "
            "INNER JOIN schema.customers b ON a.cid = b.id"
        )
        passed, reason = check_cross_join(ast)
        assert passed is True
        assert reason == ""

    def test_left_join_通过(self):
        ast = _parse_one(
            "SELECT a.id FROM schema.orders a "
            "LEFT JOIN schema.customers b ON a.cid = b.id"
        )
        assert check_cross_join(ast) == (True, "")

    def test_显式_cross_join_放行(self):
        """显式 CROSS JOIN 是成本风险（超时/内存兜底），不再语法拦截。"""
        ast = _parse_one(
            "SELECT a.id, b.name FROM schema.orders a CROSS JOIN schema.customers b"
        )
        passed, reason = check_cross_join(ast)
        assert passed is True, reason

    def test_标量广播子查询_放行(self):
        """CROSS JOIN (SELECT 聚合 ...) 是单行标量广播，合法。"""
        ast = _parse_one(
            "SELECT * FROM schema.orders "
            "CROSS JOIN (SELECT MIN(amount) AS m FROM schema.orders) s"
        )
        passed, reason = check_cross_join(ast)
        assert passed is True, reason

    def test_隐式逗号笛卡尔_被拦截(self):
        """FROM a, b 是隐式笛卡尔（无 ON）——这才是该拦的形态。"""
        ast = _parse_one("SELECT a.id, b.name FROM schema.orders a, schema.customers b")
        passed, reason = check_cross_join(ast)
        assert passed is False
        assert "隐式笛卡尔" in reason

    def test_无_on的_join_被拦截(self):
        ast = _parse_one("SELECT * FROM schema.orders JOIN schema.customers")
        passed, reason = check_cross_join(ast)
        assert passed is False
        assert "隐式笛卡尔" in reason

    def test_无_on但右侧是标量子查询_放行(self):
        ast = _parse_one(
            "SELECT * FROM schema.orders t JOIN (SELECT MAX(x) AS m FROM schema.orders) s"
        )
        passed, reason = check_cross_join(ast)
        assert passed is True, reason


# ==================== 6. SELECT * / 子查询深度 / 聚合白名单（已放行，标 disabled）====================


class TestRelaxedSyntaxRules:
    """这些语法形态是成本风险，不是安全问题：放行并在 ast_details 里标记。"""

    def test_select_star_放行并告警(self):
        allowed, reason, sanitized, details = ast_full_check("SELECT * FROM schema.users")
        assert allowed is True, reason
        assert details["select_star"]["blocked"] is False
        assert "warning" in details["select_star"]
        assert details["rules"]["select_star"]["passed"] is True
        assert "LIMIT 100" in sanitized

    def test_table_star_放行(self):
        allowed, reason, _sanitized, details = ast_full_check("SELECT t.* FROM schema.users t")
        assert allowed is True, reason
        assert "warning" in details["select_star"]

    def test_无_star_不产生告警(self):
        allowed, _reason, _sanitized, details = ast_full_check("SELECT id FROM schema.users")
        assert allowed is True
        assert details["select_star"]["blocked"] is False
        assert "warning" not in details["select_star"]

    def test_三层子查询_放行(self):
        sql = (
            "SELECT id FROM schema.a WHERE id IN ("
            "SELECT x FROM schema.b WHERE x IN ("
            "SELECT y FROM schema.c WHERE y IN ("
            "SELECT z FROM schema.d)))"
        )
        allowed, reason, _sanitized, details = ast_full_check(sql)
        assert allowed is True, reason
        assert details["subquery_depth"] == "disabled"
        assert details["rules"]["subquery_depth"]["disabled"] is True

    def test_高级聚合_放行(self):
        sql = (
            "SELECT PERCENTILE_CONT(amount, 0.5), MEDIAN(amount), "
            "COUNT_IF(x > 1), ARRAY_AGG(name) FROM schema.orders"
        )
        allowed, reason, _sanitized, details = ast_full_check(sql)
        assert allowed is True, reason
        assert details["aggregations"] == "disabled"
        assert details["rules"]["aggregations"]["disabled"] is True

    def test_窗口函数_全部放行(self):
        for func in (
            "LAG(x) OVER (ORDER BY id)",
            "LEAD(x) OVER (ORDER BY id)",
            "RANK() OVER (ORDER BY x)",
            "NTILE(4) OVER (ORDER BY x)",
            "FIRST_VALUE(x) OVER (PARTITION BY a ORDER BY b)",
            "SUM(SUM(x)) OVER ()",
        ):
            sql = f"SELECT id, {func} FROM schema.orders"
            allowed, reason, _sanitized, _details = ast_full_check(sql)
            assert allowed is True, f"{func}: {reason}"


# ==================== 8. check_dangerous_functions 危险函数黑名单 ====================


class TestCheckDangerousFunctions:
    """check_dangerous_functions：危险函数黑名单校验。"""

    def test_普通函数_通过(self):
        """普通函数如 LOWER 应通过校验。"""
        ast = _parse_one("SELECT LOWER(name) FROM schema.users")
        passed, reason = check_dangerous_functions(ast)
        assert passed is True
        assert reason == ""

    def test_聚合函数_通过(self):
        """白名单聚合函数应通过校验。"""
        ast = _parse_one("SELECT SUM(amount) FROM schema.orders")
        passed, reason = check_dangerous_functions(ast)
        assert passed is True
        assert reason == ""

    def test_read_csv_被拦截(self):
        """READ_CSV 应被拦截。"""
        ast = _parse_one("SELECT a FROM READ_CSV('/etc/passwd')")
        passed, reason = check_dangerous_functions(ast)
        assert passed is False
        assert "READ_CSV" in reason

    def test_duckdb_文件表函数家族_全部被拦截(self):
        """文件读取表函数家族必须整体拦（否则"表函数跳过 schema 校验"会打开文件通道）。"""
        family = [
            ("READ_CSV_AUTO", "SELECT a FROM read_csv_auto('C:/tmp/x.csv')"),
            ("READ_CSV", "SELECT a FROM read_csv('x.csv')"),
            ("READ_JSON", "SELECT a FROM read_json('x.json')"),
            ("READ_JSON_AUTO", "SELECT a FROM read_json_auto('x.json')"),
            ("READ_PARQUET", "SELECT a FROM read_parquet('x.parquet')"),
            ("PARQUET_SCAN", "SELECT a FROM parquet_scan('x.parquet')"),
            ("READ_TEXT", "SELECT a FROM read_text('x.txt')"),
            ("READ_BLOB", "SELECT a FROM read_blob('x.bin')"),
            ("GLOB", "SELECT a FROM glob('*.csv')"),
            ("SNIFF_CSV", "SELECT a FROM sniff_csv('x.csv')"),
            ("DELTA_SCAN", "SELECT a FROM delta_scan('x')"),
            ("ICEBERG_SCAN", "SELECT a FROM iceberg_scan('x')"),
            ("POSTGRES_SCAN", "SELECT a FROM postgres_scan('x', 't')"),
            ("MYSQL_SCAN", "SELECT a FROM mysql_scan('x', 't')"),
            ("SQLITE_SCAN", "SELECT a FROM sqlite_scan('x.db', 't')"),
        ]
        for name, sql in family:
            ast = _parse_one(sql)
            passed, reason = check_dangerous_functions(ast)
            assert passed is False, f"{name} 未被拦截: {sql}"
            assert name in reason, f"{name} 拦截原因异常: {reason}"

    def test_read_text_被拦截(self):
        """READ_TEXT 应被拦截。"""
        ast = _parse_one("SELECT READ_TEXT('file.txt')")
        passed, reason = check_dangerous_functions(ast)
        assert passed is False
        assert "READ_TEXT" in reason

    def test_write_csv_被拦截(self):
        """WRITE_CSV 应被拦截。"""
        ast = _parse_one("SELECT WRITE_CSV('out.csv')")
        passed, reason = check_dangerous_functions(ast)
        assert passed is False
        assert "WRITE_CSV" in reason

    def test_load_无法解析为函数(self):
        """LOAD 是语句级关键字，无法在 SELECT 中作为函数调用，应解析失败。"""
        # LOAD 在 DuckDB 中是语句关键字，不是可调用的函数
        # 在 SELECT 中使用会导致解析失败，由 parse_sql 层拦截
        assert parse_sql("SELECT LOAD('extension')") is None

    def test_install_无法解析为函数(self):
        """INSTALL 是语句级关键字，无法在 SELECT 中作为函数调用，应解析失败。"""
        assert parse_sql("SELECT INSTALL('httpfs')") is None

    def test_pragma_无法解析为函数(self):
        """PRAGMA 是语句级关键字，无法在 SELECT 中作为函数调用。"""
        # PRAGMA 作为独立语句会被 check_statement_type 拦截
        # 在 SELECT 中作为函数调用会导致解析失败
        assert parse_sql("SELECT PRAGMA table_info('users')") is None

    def test_set_无法解析为函数(self):
        """SET 是语句级关键字，无法在 SELECT 中作为函数调用。"""
        assert parse_sql("SELECT SET('key', 'value')") is None

    def test_call_无法解析为函数(self):
        """CALL 是语句级关键字，无法在 SELECT 中作为函数调用。"""
        assert parse_sql("SELECT CALL my_proc()") is None

    def test_read_parquet_被拦截(self):
        """READ_PARQUET 应被拦截。"""
        ast = _parse_one("SELECT * FROM READ_PARQUET('data.parquet')")
        passed, reason = check_dangerous_functions(ast)
        assert passed is False
        assert "READ_PARQUET" in reason

    def test_黑名单完整(self):
        """验证 DANGEROUS_FUNCTIONS 包含关键危险函数。"""
        key_funcs = {
            "READ_CSV", "READ_CSV_AUTO", "READ_TEXT", "WRITE_CSV",
            "PARQUET_SCAN", "GLOB", "SNIFF_CSV", "ICEBERG_SCAN",
            "ATTACH", "DETACH", "COPY", "EXPORT_DATABASE", "IMPORT_DATABASE",
            "LOAD", "INSTALL", "PRAGMA", "SET",
        }
        for func in key_funcs:
            assert func in DANGEROUS_FUNCTIONS, f"{func} 应在黑名单中"


# ==================== 9. check_table_refs 表引用格式校验 ====================


class TestCheckTableRefs:
    """check_table_refs：表引用格式校验。"""

    def test_schema_data_格式_通过(self):
        """schema.data 格式的表引用应通过。"""
        ast = _parse_one("SELECT id FROM schema.users")
        passed, reason = check_table_refs(ast)
        assert passed is True
        assert reason == ""

    def test_多表_schema_data_通过(self):
        """多个 schema.data 格式的表引用应通过。"""
        ast = _parse_one(
            "SELECT o.id, c.name "
            "FROM schema.orders o "
            "INNER JOIN schema.customers c ON o.cid = c.id"
        )
        passed, reason = check_table_refs(ast)
        assert passed is True
        assert reason == ""

    def test_子查询内_schema_data_通过(self):
        """子查询内部的 schema.data 表引用应通过。"""
        ast = _parse_one(
            "SELECT id FROM schema.users "
            "WHERE id IN (SELECT user_id FROM schema.orders)"
        )
        passed, reason = check_table_refs(ast)
        assert passed is True
        assert reason == ""

    def test_裸表名_被拦截(self):
        """不带 schema 前缀的裸表名应被拦截。"""
        ast = _parse_one("SELECT id FROM users")
        passed, reason = check_table_refs(ast)
        assert passed is False
        assert "schema" in reason.lower()

    def test_表函数_跳过schema校验(self):
        """FROM generate_series(...) 是表函数（无 schema 形态），格式层放行。"""
        ast = _parse_one("SELECT * FROM generate_series(1, 3)")
        passed, reason = check_table_refs(ast)
        assert passed is True, reason

    def test_文件路径当成表引用_仍被拒绝(self):
        """FROM 'C:/x.csv' 会被解析成无 schema 的表引用 → 必须继续拒绝。"""
        ast = _parse_one("SELECT a FROM 'C:/tmp/x.csv'")
        passed, reason = check_table_refs(ast)
        assert passed is False
        assert "schema" in reason.lower()

    def test_部分表有schema_部分没有_被拦截(self):
        """部分表有 schema 前缀、部分没有，应被拦截。"""
        ast = _parse_one(
            "SELECT o.id, u.name "
            "FROM schema.orders o "
            "INNER JOIN users u ON o.uid = u.id"
        )
        passed, reason = check_table_refs(ast)
        assert passed is False
        assert "schema" in reason.lower()

    def test_非法schema名_被拦截(self):
        """不合法的 schema 名称应被拦截。"""
        ast = _parse_one("SELECT id FROM \"123invalid\".users")
        passed, reason = check_table_refs(ast)
        assert passed is False
        assert "非法" in reason or "invalid" in reason.lower()

    def test_中文schema名_通过(self):
        """中文 schema 名（get_schema_name 从数据源名生成）是合法形态，不应被拦。

        历史 bug：标识符校验用 ^[a-zA-Z_][a-zA-Z0-9_]*$，把带中文的数据源 schema
        全部判非法 → 上传中文命名的数据源后所有查询被拦（实测踩坑）。
        修法：放行 Unicode 字母（\\w + 中文区间），仍禁引号/分号/空白/注释符/数字开头。
        """
        ast = _parse_one('SELECT id FROM "校招岗位_AI应用与Python_228bcec9"."data"')
        passed, reason = check_table_refs(ast)
        assert passed is True, reason

    def test_非法表名_被拦截(self):
        """不合法的表名应被拦截。"""
        ast = _parse_one("SELECT id FROM schema.\"123invalid\"")
        passed, reason = check_table_refs(ast)
        assert passed is False
        assert "非法" in reason or "invalid" in reason.lower()

    def test_无表引用_通过(self):
        """没有表引用的 SQL（如 SELECT 1）应通过。"""
        ast = _parse_one("SELECT 1")
        passed, reason = check_table_refs(ast)
        assert passed is True
        assert reason == ""

    def test_cte_内部表引用_校验(self):
        """CTE 定义内部的表引用应校验。"""
        # CTE 定义中的 schema.users 有 schema 前缀，应通过
        # 注意：外部引用 CTE 名称（如 x）在 SQLGlot 中也会被解析为 Table 节点，
        # 且不带 schema 前缀，所以会被拦截。这在设计上是有意为之——CTE 名不应与真实表名冲突。
        ast = _parse_one("WITH x AS (SELECT id FROM schema.users) SELECT id FROM schema.cte_ref")
        passed, reason = check_table_refs(ast)
        assert passed is True
        assert reason == ""


# ==================== 9.5 check_table_ownership 表归属白名单校验 ====================


class TestCheckTableOwnership:
    """check_table_ownership：表引用 schema 归属白名单校验。"""

    def test_本地两段表名_归属通过(self):
        """本地数据源 table_ref "schema"."data"，归属在 db，应通过。"""
        ast = _parse_one('SELECT id FROM "sch_abc123"."data"')
        passed, reason, refs = check_table_ownership(ast, {"sch_abc123"})
        assert passed is True
        assert reason == ""
        assert "sch_abc123" in refs

    def test_外部三段表名_归属通过(self):
        """外部库 table_ref "schema".public."table"，归属在 catalog，应通过。"""
        ast = _parse_one('SELECT id FROM "sch_abc123".public."orders"')
        passed, reason, refs = check_table_ownership(ast, {"sch_abc123"})
        assert passed is True
        assert reason == ""
        assert "sch_abc123" in refs

    def test_多表同源_归属通过(self):
        """多表 JOIN 且均属同一 schema，应通过。"""
        ast = _parse_one(
            'SELECT o.id, c.name FROM "sch_abc123"."orders" o '
            'INNER JOIN "sch_abc123"."customers" c ON o.cid = c.id'
        )
        passed, reason, _ = check_table_ownership(ast, {"sch_abc123"})
        assert passed is True

    def test_外来schema_被拦截(self):
        """两段表名引用外来 schema（db 位置），应拦截。"""
        ast = _parse_one('SELECT id FROM evil."data"')
        passed, reason, refs = check_table_ownership(ast, {"sch_abc123"})
        assert passed is False
        assert "evil" in reason

    def test_外来catalog_被拦截(self):
        """三段表名引用外来 catalog，应拦截。"""
        ast = _parse_one('SELECT id FROM "evil".public."data"')
        passed, reason, _ = check_table_ownership(ast, {"sch_abc123"})
        assert passed is False
        assert "evil" in reason

    def test_多表其一外来_被拦截(self):
        """多表中任一表引用外来 schema，应拦截。"""
        ast = _parse_one(
            'SELECT o.id, e.name FROM "sch_abc123"."orders" o '
            'INNER JOIN "evil"."employees" e ON o.eid = e.id'
        )
        passed, reason, _ = check_table_ownership(ast, {"sch_abc123"})
        assert passed is False

    def test_无表引用_通过(self):
        """无表引用（SELECT 1）应通过。"""
        ast = _parse_one("SELECT 1")
        passed, reason, refs = check_table_ownership(ast, {"sch_abc123"})
        assert passed is True
        assert refs == []


class TestCheckSqlTableOwnership:
    """check_sql_table_ownership：高层入口（解析 + 归属）。"""

    def test_合法sql_通过(self):
        passed, reason, _ = check_sql_table_ownership(
            'SELECT id FROM "sch_abc123"."data"', {"sch_abc123"}
        )
        assert passed is True

    def test_外来sql_拦截(self):
        passed, reason, _ = check_sql_table_ownership(
            'SELECT id FROM "other"."data"', {"sch_abc123"}
        )
        assert passed is False
        assert "other" in reason

    def test_空sql解析失败_拦截(self):
        passed, reason, _ = check_sql_table_ownership("", {"sch_abc123"})
        assert passed is False


# ==================== 10. inject_limit 自动 LIMIT 注入 ====================


class TestInjectLimit:
    """inject_limit：自动注入 LIMIT 100。"""

    def test_已有_limit_保持不变(self):
        """已有 LIMIT 子句的 SQL 应保持原样。"""
        sql = "SELECT id FROM schema.users LIMIT 50"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        assert result == sql
        assert "LIMIT 50" in result

    def test_无_limit_自动注入100(self):
        """没有 LIMIT 子句的 SQL 应自动追加 LIMIT 100。"""
        sql = "SELECT id FROM schema.users"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        assert "LIMIT 100" in result
        assert result.endswith("LIMIT 100")

    def test_limit_0_保持不变(self):
        """LIMIT 0 也应保留，不被覆盖。"""
        sql = "SELECT id FROM schema.users LIMIT 0"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        assert result == sql
        assert "LIMIT 0" in result

    def test_limit_带偏移量_保持不变(self):
        """LIMIT ... OFFSET 格式应保留。"""
        sql = "SELECT id FROM schema.users LIMIT 10 OFFSET 20"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        assert result == sql
        assert "LIMIT 10" in result

    def test_limit_小写_保持不变(self):
        """小写 limit 也应被识别并保留。"""
        sql = "select id from schema.users limit 5"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        assert result == sql
        assert "limit 5" in result

    def test_末尾分号_正确处理(self):
        """末尾带分号的 SQL 应正确处理：分号前追加 LIMIT。"""
        sql = "SELECT id FROM schema.users;"
        ast = _parse_one(sql.rstrip(";"))
        result = inject_limit(sql, ast)
        # 分号应该被移除，LIMIT 追加在后面
        assert result.endswith("LIMIT 100")
        assert ";" not in result.rstrip().rstrip(";")

    def test_字符串中含有limit关键字_不被误判(self):
        """字符串字面量中的 LIMIT 不应被误判为已有 LIMIT。"""
        sql = "SELECT name FROM schema.users WHERE name = 'this has limit in it'"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        # 应注入 LIMIT 100
        assert "LIMIT 100" in result

    def test_limit_在注释中_不被误判(self):
        """注释中的 LIMIT 关键字应被正确识别（当前实现会在原始 SQL 中匹配注释文本）。"""
        # 注意：当前实现中 inject_limit 对原始 SQL 字符串做正则匹配
        # 未剥离注释，因此注释中的 "limit" 会被匹配到，SQL 保持不变
        # 这是已知的局限性，DBA 审查时应留意
        sql = "SELECT id FROM schema.users /* this has limit in comment */"
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        # 注释中的 "limit" 被正则匹配到，当前实现不会注入
        assert result == sql

    def test_复杂sql_无limit_注入(self):
        """复杂 SQL 无 LIMIT 时应注入。"""
        sql = (
            "SELECT o.id, SUM(o.amount) AS total "
            "FROM schema.orders o "
            "INNER JOIN schema.customers c ON o.cid = c.id "
            "WHERE c.status = 'active' "
            "GROUP BY o.id "
            "ORDER BY total DESC"
        )
        ast = _parse_one(sql)
        result = inject_limit(sql, ast)
        assert "LIMIT 100" in result


# ==================== 11. ast_full_check 全量校验入口（集成测试）====================


class TestAstFullCheck:
    """ast_full_check：全量校验入口，执行完整 pipeline。"""

    def test_合法_sql_通过(self):
        """合法的 SELECT 查询应通过所有校验。"""
        sql = "SELECT id, name FROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""
        assert sanitized is not None
        assert "LIMIT 100" in sanitized
        assert details is not None
        assert details["statement_type"] == "Select"
        assert details["has_limit"] is False  # 注入前无 LIMIT

    def test_合法_sql_已有limit_通过(self):
        """已有 LIMIT 的合法 SELECT 应通过，保留原 LIMIT。"""
        sql = "SELECT id, name FROM schema.users LIMIT 50"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""
        assert sanitized == sql
        assert details is not None
        assert details["has_limit"] is True

    def test_空_sql_返回_false(self):
        """空 SQL 应返回 False。"""
        allowed, reason, sanitized, details = ast_full_check("")
        assert allowed is False
        assert reason == "SQL 为空"
        assert sanitized == ""

    def test_仅空白_sql_返回_false(self):
        """仅含空白字符的 SQL 应返回 False。"""
        allowed, reason, sanitized, details = ast_full_check("   \n  ")
        assert allowed is False
        assert "为空" in reason

    def test_仅注释_sql_返回_false(self):
        """仅含注释的 SQL 应返回 False。"""
        allowed, reason, sanitized, details = ast_full_check("-- 这是一条注释")
        assert allowed is False
        assert "注释" in reason

    def test_畸形_sql_解析失败_fail_closed拦截(self):
        """SQLGlot 无法解析的 SQL 一律拒绝（fail-closed）。

        解析失败可能源于恶意/畸形语句，也可能源于方言覆盖不全；
        安全策略为「解析异常一律拒绝」，绝不降级放行到执行层。
        """
        sql = "SELECT FROM WHERE"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "解析失败" in reason
        assert details is not None and details.get("parse_error")
        assert details.get("fail_closed") is True

    def test_多语句_sql_返回_false(self):
        """多条 SQL 语句应被拦截。"""
        sql = "SELECT 1; SELECT 2"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "多条" in reason

    def test_危险函数_返回_false_带原因(self):
        """含危险函数的 SQL 应返回 False 并给出正确原因。"""
        sql = "SELECT READ_CSV('file.csv')"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "READ_CSV" in reason
        assert details is not None
        assert details["failed_rule"] == "dangerous_functions"

    def test_select_star_放行并告警(self):
        """SELECT * 不再拦截：只记 warning（成本风险交给超时/内存上限）。"""
        sql = "SELECT * FROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True, reason
        assert details["select_star"]["blocked"] is False
        assert "warning" in details["select_star"]

    def test_union_放行(self):
        """UNION / EXCEPT / INTERSECT 放行：表归属校验会遍历所有分支。"""
        for sql in (
            "SELECT id FROM schema.users UNION SELECT id FROM schema.admins",
            "SELECT id FROM schema.users UNION ALL SELECT id FROM schema.admins",
            "SELECT id FROM schema.users EXCEPT SELECT id FROM schema.admins",
        ):
            allowed, reason, _sanitized, details = ast_full_check(sql)
            assert allowed is True, f"{sql}: {reason}"
            assert details["statement_type"] == "Union" or details["statement_type"] == "Except"

    def test_显式_cross_join_放行(self):
        sql = "SELECT * FROM schema.orders CROSS JOIN schema.customers"
        allowed, reason, _sanitized, _details = ast_full_check(sql)
        assert allowed is True, reason

    def test_隐式笛卡尔_被拦截(self):
        sql = "SELECT * FROM schema.orders, schema.customers"
        allowed, reason, _sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "隐式笛卡尔" in reason
        assert details["failed_rule"] == "cross_join"

    def test_子查询超深_放行(self):
        """子查询深度限制已删除（零安全价值）：多层级联查询必须能跑。"""
        sql = (
            "SELECT id FROM schema.users "
            "WHERE id IN ("
            "  SELECT user_id FROM schema.orders "
            "  WHERE order_id IN ("
            "    SELECT order_id FROM schema.items "
            "    WHERE item_id IN (SELECT item_id FROM schema.inventory)"
            "  )"
            ")"
        )
        allowed, reason, _sanitized, details = ast_full_check(sql)
        assert allowed is True, reason
        assert details["subquery_depth"] == "disabled"

    def test_裸表名_被拦截(self):
        """不带 schema 前缀的表引用应被拦截。"""
        sql = "SELECT id FROM users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "schema" in reason.lower()
        assert details["failed_rule"] == "table_refs"

    def test_ast_details_包含所有规则结果(self):
        """通过时 ast_details 应包含所有规则的校验结果。"""
        sql = "SELECT id, name FROM schema.users LIMIT 10"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert details is not None
        assert "rules" in details
        # 验证所有规则都在 details 中
        expected_rules = [
            "statement_type", "dangerous_functions", "cross_join",
            "table_refs", "subquery_depth", "aggregations", "select_star",
        ]
        for rule in expected_rules:
            assert rule in details["rules"], f"规则 {rule} 应出现在 details 中"
            assert "passed" in details["rules"][rule]
            assert details["rules"][rule]["passed"] is True

    def test_失败时_ast_details_包含失败规则(self):
        """拦截时 ast_details 应包含 failed_rule 和 failed_reason。"""
        # 使用不受 schema 前缀保护的裸表名触发 table_refs 失败
        sql = "SELECT id FROM users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert details["failed_rule"] == "table_refs"
        assert details["failed_reason"] is not None

    def test_with_cte_合法_通过(self):
        """合法的 WITH ... SELECT 应通过（CTE 外部引用有 schema 的表）。"""
        sql = "WITH x AS (SELECT id FROM schema.users) SELECT id FROM schema.cte_data x"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""
        assert "LIMIT 100" in sanitized


# ==================== 12. 边界与异常情况 ====================


class TestEdgeCases:
    """边界与异常情况测试。"""

    # ---- SQL 注释 ----

    def test_sql_含单行注释_可解析(self):
        """含 -- 单行注释的 SQL 应能正确解析并校验。"""
        sql = "SELECT id, name -- 注释内容\nFROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    def test_sql_含多行注释_可解析(self):
        """含 /* */ 多行注释的 SQL 应能正确解析并校验。"""
        sql = "SELECT id /* 多行\n注释 */ FROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    def test_sql_含注释和字符串_不误判(self):
        """注释中的关键字不应被误判。"""
        sql = (
            "SELECT id FROM schema.users "
            "WHERE name = '-- not a comment'"
        )
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    def test_纯注释_返回_false(self):
        """纯注释 SQL 应返回 False。"""
        sql = "-- 仅注释\n/* 更多注释 */"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "注释" in reason

    # ---- 字符串字面量包含关键字 ----

    def test_字符串含_union_不误判(self):
        """字符串字面量中的 UNION 不应被误判为集合操作。"""
        sql = "SELECT id, name FROM schema.users WHERE name = 'UNION SELECT'"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    def test_字符串含_drop_不误判(self):
        """字符串字面量中的 DROP 不应被误判。"""
        sql = "SELECT id FROM schema.users WHERE name = 'drop table'"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    def test_字符串含_select_star_不误判(self):
        """字符串字面量中的 * 不应被误判为 SELECT *。"""
        sql = "SELECT id FROM schema.users WHERE name = '*'"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    # ---- 超长 SQL ----

    def test_超长_sql_不崩溃(self):
        """超长 SQL 不应导致崩溃。"""
        long_cols = ", ".join([f"col_{i}" for i in range(100)])
        sql = f"SELECT {long_cols} FROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        # 应注入 LIMIT 100
        assert "LIMIT 100" in sanitized

    def test_超长_sql_含危险函数_仍能拦截(self):
        """超长 SQL 含危险函数仍应被正确拦截。"""
        long_cols = ", ".join([f"col_{i}" for i in range(100)])
        sql = f"SELECT {long_cols}, READ_CSV('file') FROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert "READ_CSV" in reason

    # ---- 仅空白字符 ----

    def test_仅空白字符_via_parse_sql(self):
        """仅空白字符的 SQL 通过 parse_sql 应返回 None。"""
        assert parse_sql("   \t  ") is None

    def test_仅换行符_via_parse_sql(self):
        """仅换行符的 SQL 通过 parse_sql 应返回 None。"""
        assert parse_sql("\n\n\n") is None

    # ---- 特殊 SQL 构造 ----

    def test_case_when_不崩溃(self):
        """CASE WHEN 表达式不应导致崩溃。"""
        sql = (
            "SELECT id, "
            "CASE WHEN status = 'active' THEN '是' ELSE '否' END AS is_active "
            "FROM schema.users"
        )
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    def test_window_function_不崩溃(self):
        """窗口函数不应导致崩溃。"""
        sql = (
            "SELECT id, amount, "
            "ROW_NUMBER() OVER (ORDER BY amount DESC) AS rank "
            "FROM schema.orders"
        )
        ast = _parse_one(sql)
        # 窗口函数不在任何黑名单里，应通过；聚合白名单已删除（disabled）
        passed, reason = check_dangerous_functions(ast)
        assert passed is True, reason
        allowed, reason, _sanitized, details = ast_full_check(sql)
        assert allowed is True, reason
        assert details["aggregations"] == "disabled"

    def test_带别名_through(self):
        """带别名的查询应通过。"""
        sql = "SELECT o.id AS order_id, c.name AS customer_name FROM schema.orders o INNER JOIN schema.customers c ON o.cid = c.id"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert reason == ""

    # ---- 空值/None 输入 ----

    def test_parse_sql_none_输入(self):
        """parse_sql 接收 None 应返回 None（不崩溃）。"""
        # 类型提示是 str，但如果传 None 进来，实际行为取决于实现
        # 由于实现检查了 not sql，所以 None 应触发空字符串检查
        result = parse_sql(None)  # type: ignore
        assert result is None

    def test_ast_full_check_none_输入(self):
        """ast_full_check 接收 None 应返回 False（不崩溃）。"""
        allowed, reason, sanitized, details = ast_full_check(None)  # type: ignore
        assert allowed is False
        assert "为空" in reason


# ==================== 13. 规则优先级与组合场景 ====================


class TestRulePriority:
    """ast_full_check 中多个规则同时触发时的行为。"""

    def test_多条规则同时违规_返回首个失败(self):
        """多条规则同时违规时，应返回 checks 顺序里的第一个失败规则。

        新顺序：statement_type → dangerous_functions → cross_join → table_refs
        （allowed_schemas 传入时再追加 table_ownership）。
        """
        sql = (
            "SELECT a FROM read_csv_auto('x.csv') CROSS JOIN "
            "schema.orders, schema.customers"
        )
        allowed, reason, _sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert details is not None
        assert details["failed_rule"] == "dangerous_functions", details.get("failed_rule")

    def test_危险函数优先于表引用(self):
        sql = "SELECT a FROM users, read_csv('x.csv')"
        allowed, _reason, _sanitized, details = ast_full_check(sql)
        assert allowed is False
        assert details["failed_rule"] == "dangerous_functions"

    def test_先通过后违规_顺序正确(self):
        """先通过所有语法检查，再被 LIMIT 注入，不应出错。"""
        sql = "SELECT id, name FROM schema.users"
        allowed, reason, sanitized, details = ast_full_check(sql)
        assert allowed is True
        assert "LIMIT 100" in sanitized
        # 验证 LIMIT 只出现一次
        assert sanitized.count("LIMIT") == 1

# ==================== 14. 放宽后的回归集（P3-A 验收：真实被拦 SQL 全部解锁）====================

_PASS_SQLS = [
    'SELECT a FROM "s"."data"',
    'SELECT id, LAG(x) OVER (ORDER BY id) FROM "s"."data"',
    'SELECT LEAD(x) OVER (ORDER BY id) AS n FROM "s"."data"',
    'SELECT RANK() OVER (ORDER BY x) AS r FROM "s"."data"',
    'SELECT NTILE(4) OVER (ORDER BY x) FROM "s"."data"',
    'SELECT FIRST_VALUE(x) OVER (PARTITION BY a ORDER BY b) FROM "s"."data"',
    'SELECT SUM(SUM(x)) OVER () FROM "s"."data"',
    'SELECT PERCENTILE_CONT(x, 0.5) FROM "s"."data"',
    'SELECT CASE WHEN x > 1 THEN \'a\' ELSE \'b\' END AS seg FROM "s"."data"',
    'WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT * FROM a JOIN b ON TRUE',
    'SELECT id FROM "s"."a" UNION ALL SELECT id FROM "s"."b"',
    'PIVOT "s"."data" ON a USING SUM(b)',
    'SELECT * FROM "s"."data" CROSS JOIN (SELECT MIN(x) AS m FROM "s"."data") s',
    'SELECT * FROM generate_series(1, 3)',
    'SELECT COUNT_IF(x > 1) FROM "s"."data"',
    'SELECT * FROM (SELECT x, ROW_NUMBER() OVER () rn FROM "s"."data") t WHERE rn <= 3',
    "SELECT id FROM data",   # 裸表名 + known_tables 自动补全
]

_REJECT_SQLS = [
    "SELECT 1; SELECT 2",
    'INSERT INTO "s"."data" VALUES (1)',
    'UPDATE "s"."data" SET a = 1',
    'DROP TABLE "s"."data"',
    "SELECT a FROM read_csv_auto('C:/tmp/x.csv')",
    "SELECT a FROM read_csv('x.csv')",
    "SELECT a FROM parquet_scan('x.parquet')",
    "SELECT a FROM glob('*.csv')",
    "SELECT a FROM sniff_csv('x.csv')",
    "SELECT a FROM 'C:/tmp/x.csv'",
    'SELECT id FROM "tenant_other"."data"',
    'SELECT * FROM "s"."a", "s"."b"',
    'SELECT id FROM users',   # 未知名：不猜、不补、拒绝
]


class TestRelaxedRegression:
    @pytest.mark.parametrize("sql", _PASS_SQLS)
    def test_应通过(self, sql):
        allowed, reason, _sanitized, _details = ast_full_check(
            sql, allowed_schemas={"s"}, known_tables={"data"}
        )
        assert allowed is True, f"应放行却被拦: {sql} -> {reason}"

    @pytest.mark.parametrize("sql", _REJECT_SQLS)
    def test_应拒绝(self, sql):
        allowed, reason, _sanitized, _details = ast_full_check(
            sql, allowed_schemas={"s"}, known_tables={"data"}
        )
        assert allowed is False, f"应拦截却放行: {sql}"
        assert reason, f"拦截必须给原因: {sql}"


# ==================== 15. 裸表名自动补全 ====================


class TestBareTableAutocomplete:
    def _chk(self, sql, schemas, known):
        return ast_full_check(sql, allowed_schemas=schemas, known_tables=known)

    def test_单schema已知表_补全并通过(self):
        allowed, reason, sanitized, details = self._chk(
            "SELECT id FROM data", {"s"}, {"data"}
        )
        assert allowed is True, reason
        assert details["completed_tables"] == ["s.data"]
        assert '"s"' in sanitized.split("FROM", 1)[1]

    def test_外部库表名_也可补全(self):
        allowed, reason, _sanitized, details = self._chk(
            'SELECT id FROM orders', {"s"}, {"orders"}
        )
        assert allowed is True, reason
        assert details["completed_tables"] == ["s.orders"]

    def test_未知名_不补仍拒绝(self):
        allowed, reason, _sanitized, details = self._chk(
            "SELECT id FROM users", {"s"}, {"data"}
        )
        assert allowed is False
        assert details["failed_rule"] == "table_refs"
        assert "schema" in reason.lower()

    def test_无known_tables_保持旧行为拒绝(self):
        allowed, _reason, _sanitized, details = self._chk(
            "SELECT id FROM data", {"s"}, None
        )
        assert allowed is False
        assert details["failed_rule"] == "table_refs"

    def test_多schema_不补仍拒绝(self):
        allowed, _reason, _sanitized, details = self._chk(
            "SELECT id FROM data", {"s", "s2"}, {"data"}
        )
        assert allowed is False
        assert details["failed_rule"] == "table_refs"

    def test_已带schema_不重复补全(self):
        allowed, reason, _sanitized, details = self._chk(
            'SELECT id FROM "s"."data"', {"s"}, {"data"}
        )
        assert allowed is True, reason
        assert not details.get("completed_tables")


# ==================== 16. 敏感列旁路（agent_tools AST 版）====================


class TestSensitiveColumnBypass:
    """旧实现是 SQL 文本正则；SELECT * 不出现列名即可绕过。新实现解析 AST 求交。"""

    META = {"fields": [{"name": "phone", "sensitive": True}, {"name": "region"}]}

    @staticmethod
    def _hits(sql, sensitive=None):
        from app.services.agent_tools import QueryDatasourceTool

        return QueryDatasourceTool._sensitive_columns_in_sql(
            sql, sensitive or {"phone"}, TestSensitiveColumnBypass.META
        )

    def test_显式敏感列被拦(self):
        assert self._hits('SELECT phone FROM "s"."data"') == ["phone"]

    def test_select_star_命中敏感列(self):
        assert self._hits('SELECT * FROM "s"."data"') == ["phone"]

    def test_表前缀星号_命中敏感列(self):
        assert self._hits('SELECT t.* FROM "s"."data" t') == ["phone"]

    def test_表达式里的字段名_命中敏感列(self):
        assert self._hits('SELECT SUM("phone") FROM "s"."data"') == ["phone"]

    def test_无敏感列时_select_star_不拦(self):
        meta_only_region = {"fields": [{"name": "region"}]}
        from app.services.agent_tools import QueryDatasourceTool

        hits = QueryDatasourceTool._sensitive_columns_in_sql(
            'SELECT * FROM "s"."data"', {"phone"}, meta_only_region
        )
        assert hits == []

    def test_解析失败_退回正则(self):
        assert self._hits("SELECT phone FROM") == ["phone"]
