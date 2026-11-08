"""基于 AST 的 SQL 安全校验层（L4 - 语法树级防护）。

设计定位（业界调研结论，docs/research/10-sql-guardrails-industry.md）：
AST 白名单**不是安全边界**，只是"快速失败 + 友好报错"；真正的边界在只读连接 /
RLS / 列权限。因此这里只保留**有安全语义**的规则，语法形态（UNION / CROSS JOIN /
SELECT * / 窗口函数 / 子查询深度 / 聚合白名单）一律不拦——它们是成本风险，
交给超时与内存上限兜底。

保留的规则：
    1. 语句类型 —— 仅允许查询（SELECT / WITH / 集合运算 / PIVOT）
    2. 单语句
    3. 危险函数/关键字 —— 文件读取、写导出、挂载/系统调用（含 DuckDB 表函数家族）
    4. 表归属 schema 白名单 + 表引用格式（防跨租户）
    5. 自动 LIMIT 注入（成本兜底）

已删除（零安全价值 + sqlglot 名字映射会持续漂移）：
    子查询深度上限、raw SQL 聚合函数白名单、SELECT * 拦截、UNION/EXCEPT/INTERSECT 拦截。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger("lvco.sql_guard_ast")

# ==================== 常量定义 ====================

# 危险函数黑名单 —— 文件/远程读取、写导出、挂载/系统调用。
# 注意：DuckDB 的文件读取"表函数"在 AST 里是 exp.Table(this=exp.Func)。若只让表函数
# 跳过 schema 校验而不拦函数名，等于打开任意文件读取通道——两条规则必须成组存在。
DANGEROUS_FUNCTIONS = frozenset({
    # 文件 / 远程读取（DuckDB 表函数家族）
    "READ_CSV", "READ_CSV_AUTO", "READ_JSON", "READ_JSON_AUTO",
    "READ_PARQUET", "READ_TEXT", "READ_BLOB",
    "PARQUET_SCAN", "GLOB", "SNIFF_CSV",
    "DELTA_SCAN", "ICEBERG_SCAN",
    "POSTGRES_SCAN", "MYSQL_SCAN", "SQLITE_SCAN",
    # 写 / 导出
    "WRITE_CSV", "WRITE_PARQUET", "WRITE_JSON",
    "COPY_DATABASE", "COPY", "EXPORT_DATABASE", "IMPORT_DATABASE",
    # 挂载 / 系统 / 配置
    "ATTACH", "DETACH", "CALL", "LOAD", "INSTALL", "PRAGMA", "SET",
})

# 默认注入的 LIMIT 值（成本兜底）
_DEFAULT_LIMIT = 100


# ==================== 核心函数 ====================


def parse_sql(sql: str) -> list[sqlglot.exp.Expression] | None:
    """解析 SQL 字符串为 AST 节点列表（DuckDB 方言）。

    Args:
        sql: 原始 SQL 字符串。

    Returns:
        解析后的 AST 表达式列表，解析失败时返回 None。
    """
    if not sql or not sql.strip():
        return None
    try:
        # 先去除 SQL 注释，避免注释干扰解析
        cleaned = _strip_comments(sql)
        if not cleaned or not cleaned.strip():
            return None
        return sqlglot.parse(cleaned, dialect="duckdb")
    except sqlglot.errors.ParseError:
        # 解析失败时直接返回 None，不抛异常
        return None
    except Exception:
        # 兜底捕获其他异常（如内存溢出等极端情况）
        return None


def check_statement_type(ast: exp.Expression) -> tuple[bool, str]:
    """校验 SQL 语句类型：仅允许查询类语句。

    允许：
      - SELECT / WITH...SELECT
      - 集合运算（UNION / EXCEPT / INTERSECT）—— 分支必须都是查询；
        表归属校验会遍历全部分支，集合操作本身不构成提权。
      - PIVOT —— 行列转换，包装的仍是查询。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    if isinstance(ast, exp.With):
        if ast.find(exp.Select) is None:
            return False, "仅允许 SELECT 查询：WITH 语句中未找到 SELECT"
        return True, ""
    if isinstance(ast, exp.Select):
        return True, ""
    if isinstance(ast, exp.Pivot):
        return True, ""
    if isinstance(ast, (exp.Union, exp.Except, exp.Intersect)):
        if not _set_operation_branches_are_queries(ast):
            return False, "集合操作仅允许由 SELECT 查询组成"
        return True, ""
    return False, f"仅允许 SELECT 查询，当前语句类型: {type(ast).__name__}"


def _set_operation_branches_are_queries(node: exp.Expression | None) -> bool:
    """集合运算的每个分支都必须是查询（递归处理嵌套 UNION）。"""
    if node is None:
        return False
    if isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
        branches = [node.args.get(k) for k in ("this", "expression") if node.args.get(k) is not None]
        return bool(branches) and all(_set_operation_branches_are_queries(b) for b in branches)
    if isinstance(node, exp.Subquery):
        return _set_operation_branches_are_queries(node.this)
    return isinstance(node, (exp.Select, exp.With, exp.Pivot))


def check_cross_join(ast: exp.Expression) -> tuple[bool, str]:
    """只拦"隐式笛卡尔积"，不拦显式 CROSS JOIN。

    规则：
      - Join 没有 ON，且 kind/method 都不是 CROSS → 隐式笛卡尔（如 FROM a, b），拦截；
      - 右侧是"单行聚合子查询"（无 GROUP BY/HAVING 且含聚合）→ 标量广播，放行；
      - 显式 CROSS JOIN → 成本风险交给超时/内存上限，不拦。

    Returns:
        (是否通过, 拦截原因)。
    """
    for join_node in ast.find_all(exp.Join):
        if join_node.args.get("on") is not None:
            continue
        kind = join_node.args.get("kind")
        method = join_node.args.get("method")
        kind_s = str(kind).upper() if kind is not None else ""
        method_s = str(method).upper() if method is not None else ""
        if kind_s == "CROSS" or method_s == "CROSS":
            continue
        if _is_scalar_aggregate_subquery(join_node.args.get("this")):
            continue
        return False, (
            "检测到隐式笛卡尔积（JOIN 缺少 ON 条件）；"
            "请改为显式 JOIN ... ON，或使用标量子查询（CROSS JOIN (SELECT 聚合 ...)）"
        )
    return True, ""


def _is_scalar_aggregate_subquery(node: exp.Expression | None) -> bool:
    """右侧是否为"单行聚合子查询"（无 GROUP BY/HAVING 且含聚合函数）。"""
    sub: exp.Subquery | None = None
    if isinstance(node, exp.Subquery):
        sub = node
    elif isinstance(node, exp.Table) and isinstance(node.this, exp.Subquery):
        sub = node.this
    if sub is None:
        return False
    select = sub.find(exp.Select)
    if not isinstance(select, exp.Select):
        return False
    if select.args.get("group") is not None or select.args.get("having") is not None:
        return False
    return select.find(exp.AggFunc) is not None


def _function_names(func: exp.Func) -> set[str]:
    """收集函数节点的候选名字（sql_name / 类名 / 原名），全大写供黑名单比对。"""
    names: set[str] = set()
    try:
        names.add(str(func.sql_name()).upper())
    except Exception:  # noqa: BLE001
        pass
    names.add(type(func).__name__.upper())
    raw = getattr(func, "name", None)
    if isinstance(raw, str) and raw:
        names.add(raw.upper())
    return names


def _is_table_function(table: exp.Table) -> bool:
    """表引用是否其实是表函数（FROM read_csv(...) / generate_series(...)）。

    SQLGlot 把 FROM f(...) 解析成 exp.Table，其 this 是 exp.Func；它没有 schema
    前缀，若不识别会被"表引用缺 schema"误拦。安全前提：危险函数家族已先被
    黑名单拦住（两条规则必须成组存在）。
    """
    return isinstance(table.this, exp.Func)


def _has_bare_star(ast: exp.Expression) -> bool:
    """是否存在裸 Star 投影（SELECT * / t.*）——不再拦截，只做警告标记。"""
    for select_node in ast.find_all(exp.Select):
        for proj in select_node.expressions:
            expr = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(expr, exp.Star):
                return True
            if isinstance(expr, exp.Column) and isinstance(expr.this, exp.Star):
                return True
    return False


def check_dangerous_functions(ast: exp.Expression) -> tuple[bool, str]:
    """检查危险函数（文件/远程读取、写导出、挂载/系统调用）。

    覆盖 DuckDB 文件表函数家族（READ_CSV_AUTO / PARQUET_SCAN / GLOB / SNIFF_CSV ...
    在 AST 里是 exp.Table(this=exp.Func) 或 exp.Anonymous）。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    for func in ast.find_all(exp.Func):
        for name in _function_names(func):
            if name in DANGEROUS_FUNCTIONS:
                return False, f"SQL 包含危险函数: '{name}'"
    return True, ""


def _collect_cte_names(ast: exp.Expression) -> set[str]:
    """收集 WITH 子句中定义的 CTE 名称。

    SQLGlot 把 `FROM cte` 中的 CTE 引用解析为 exp.Table（无 db 前缀），
    若不加区分会把合法的 CTE 引用误判为"裸表名"。此函数先收集所有
    WITH 定义的 CTE 名，供 check_table_refs 跳过校验。
    """
    names: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        alias = cte.args.get("alias")
        if isinstance(alias, exp.TableAlias):
            n = alias.this
            if hasattr(n, "name"):
                names.add(n.name)
            elif n is not None:
                names.add(str(n))
        elif alias is not None and hasattr(alias, "name"):
            names.add(alias.name)
    return names


def check_table_refs(ast: exp.Expression) -> tuple[bool, str]:
    """检查表引用是否为两段式 schema.data（防跨租户）。

    - 表函数（FROM read_csv(...) / generate_series(...)）跳过：它们没有 schema 形态，
      文件读取由危险函数黑名单负责（两条规则成组存在）。
    - CTE 引用（FROM cte）跳过。
    - 其余裸表名（FROM users / FROM 'C:/x.csv'）一律拒绝。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    cte_names = _collect_cte_names(ast)
    for table in ast.find_all(exp.Table):
        if _is_table_function(table):
            continue
        table_name = table.name
        db = table.args.get("db")

        if table_name in cte_names:
            continue

        if db is None:
            return False, (
                f"表引用 '{table.sql(dialect='duckdb')}' 缺少 schema 前缀，"
                f"格式应为 schema.data"
            )

        db_name = db.name if hasattr(db, "name") else str(db)
        if not _is_valid_identifier(db_name) or not _is_valid_identifier(table_name):
            return False, (
                f"表引用 '{table.sql(dialect='duckdb')}' 包含非法标识符"
            )

    return True, ""


def _complete_bare_tables(
    ast: exp.Expression,
    allowed_schemas: set[str] | None,
    known_tables: set[str] | None,
) -> list[str]:
    """裸表名自动补全：仅当"单 schema + 表名在已知表集合里"时补前缀。

    只补已知表名，**绝不猜未知表名**；补不了的原样留给 check_table_refs 拒绝。
    返回实际补全的表引用（用于 ast_details 可观测）。
    """
    if not allowed_schemas or len(allowed_schemas) != 1 or not known_tables:
        return []
    schema_name = next(iter(allowed_schemas))
    known_lower = {str(name).lower() for name in known_tables if name}
    cte_names = _collect_cte_names(ast)
    completed: list[str] = []
    for table in ast.find_all(exp.Table):
        if _is_table_function(table):
            continue
        if table.name in cte_names:
            continue
        if table.args.get("db") is not None or table.args.get("catalog") is not None:
            continue
        if str(table.name or "").lower() not in known_lower:
            continue
        table.set("db", exp.to_identifier(str(schema_name), quoted=True))
        completed.append(f"{schema_name}.{table.name}")
    if completed:
        logger.info("[ast_full_check] 裸表名自动补全: %s -> schema=%s", completed, schema_name)
    return completed


def check_table_ownership(ast: exp.Expression, allowed_schemas: set[str]) -> tuple[bool, str, list[str]]:
    """校验所有表引用的 schema 归属白名单。

    表引用格式已由 check_table_refs 保证为两段（schema.table）或三段
    （catalog.schema.table）。归属标识取 catalog（若有）否则 db：
    - 本地数据源 table_ref 为 "schema"."data"，归属标识在 db；
    - 外部库 table_ref 为 "schema".public."table"，归属标识在 catalog。

    仅校验归属、不重复格式校验；无归属标识的表引用（裸表名 / CTE 名）
    交由 check_table_refs 拦截，此处放行。

    Args:
        ast: 解析后的 AST 根节点。
        allowed_schemas: 当前数据源允许的 schema 名集合（含当前 schema_name）。

    Returns:
        (是否通过, 拦截原因, 引用到的归属标识列表)。通过时 reason 为空字符串。
    """
    referenced: list[str] = []
    for table in ast.find_all(exp.Table):
        catalog = table.args.get("catalog")
        db = table.args.get("db")
        owner = catalog if catalog is not None else db
        if owner is None:
            continue  # 裸表 / CTE 引用：由格式层 check_table_refs 拦截
        owner_name = owner.name if hasattr(owner, "name") else str(owner)
        referenced.append(owner_name)
        if owner_name not in allowed_schemas:
            return False, (
                f"表引用 '{table.sql(dialect='duckdb')}' 的 schema '{owner_name}' "
                f"不属于当前数据源（允许的 schema: {sorted(allowed_schemas)}）"
            ), referenced
    return True, "", referenced


def _check_ownership(ast: exp.Expression, allowed_schemas: set[str]) -> tuple[bool, str]:
    """check_table_ownership 的二元组包装（供 ast_full_check 的规则列表使用）。"""
    ok, reason, _refs = check_table_ownership(ast, allowed_schemas)
    return ok, reason


def check_sql_table_ownership(
    sql: str,
    allowed_schemas: set[str],
) -> tuple[bool, str, list[str]]:
    """对完整 SQL 字符串执行表归属白名单校验（封装解析 + 归属检查）。

    Args:
        sql: 待校验的 SQL 字符串。
        allowed_schemas: 当前数据源允许的 schema 名集合。

    Returns:
        (是否通过, 拦截原因, 引用到的归属标识列表)。
        解析失败时降级放行（交由 DuckDB 只读执行兜底），避免误拦正确查询。
    """
    parsed = parse_sql(sql)
    if parsed is None:
        # 空 SQL / 纯注释：没有可执行内容，必须拦截。
        if not sql or not sql.strip():
            return False, "SQL 为空", []
        # fail-closed：解析失败 = 无法证明表归属合法，一律拒绝。
        # （ast_full_check 在更前面已 fail-closed；两层语义保持一致，绝不降级放行。）
        logger.warning("[check_sql_table_ownership] SQL 解析失败，拒绝执行(fail-closed): %s", sql[:200])
        return False, "SQL 解析失败，无法校验表归属（已拒绝执行）", []
    valid = [s for s in parsed if s is not None]
    if len(valid) != 1:
        return False, f"SQL 语句数异常（{len(valid)} 条），无法校验表归属", []
    return check_table_ownership(valid[0], allowed_schemas)


def inject_limit(sql: str, ast: exp.Expression) -> str:
    """检查 SQL 是否包含 LIMIT 子句，缺失时自动注入 LIMIT 100。

    Args:
        sql: 原始 SQL 字符串。
        ast: 解析后的 AST 根节点。

    Returns:
        处理后的 SQL 字符串（可能追加了 LIMIT 子句）。
    """
    # PIVOT 语句形态不支持直接追加 LIMIT；成本风险交给查询超时兜底
    if isinstance(ast, exp.Pivot):
        return sql

    # 检查 AST 中是否已有 LIMIT 子句
    if ast.find(exp.Limit) is not None:
        return sql

    # 检查原始 SQL 字符串中是否包含 LIMIT（防止 AST 解析遗漏的情况）
    # 使用正则匹配 LIMIT 关键字（排除字符串字面量中的 LIMIT）
    sql_no_strings = _remove_string_literals(sql)
    if re.search(r"\bLIMIT\b", sql_no_strings, re.IGNORECASE):
        return sql

    # 去除末尾可能的分号和空白，追加 LIMIT
    sql = sql.rstrip().rstrip(";").rstrip()
    return f"{sql}\nLIMIT {_DEFAULT_LIMIT}"


def ast_full_check(
    sql: str,
    *,
    allowed_schemas: set[str] | None = None,
    known_tables: set[str] | None = None,
) -> tuple[bool, str, str, dict | None]:
    """AST 全量校验入口，执行完整的语法树安全检查 pipeline。

    校验流程：
    1. 解析 SQL 为 AST（解析失败 → fail-closed）
    2. 多语句检查
    3. 裸表名自动补全（仅当 allowed_schemas 恰好 1 个且 known_tables 命中）
    4. 逐项规则校验（语句类型 / 危险函数 / 隐式笛卡尔 / 表引用格式）
    5. 自动 LIMIT 注入
    6. 汇总可观测信息（已删除规则显式标 disabled；SELECT * 仅告警）

    Args:
        sql: 待校验的原始 SQL。
        allowed_schemas: 当前数据源允许的 schema 集合（自动补全用；默认 None 关闭）。
        known_tables: 当前数据源已知表名集合（自动补全用；默认 None 表示不补）。

    Returns:
        (是否放行, 拦截原因, 净化后的 SQL, AST 详细信息)。
    """
    # 空 SQL 直接拦截
    if not sql or not sql.strip():
        return False, "SQL 为空", "", None

    # 剥离注释后再检查是否为空（纯注释 SQL 视为空）
    cleaned = _strip_comments(sql)
    if not cleaned or not cleaned.strip():
        return False, "SQL 仅包含注释，无有效语句", "", None

    # ---- 步骤 1: 解析 ----
    parsed = parse_sql(sql)
    if parsed is None:
        logger.warning("[ast_full_check] SQL 解析失败，拒绝执行(fail-closed): %s", sql[:200])
        return False, "SQL 解析失败（可能含非法或不支持的语法），已拒绝执行", sql, {
            "parse_error": str(sql[:200]),
            "fail_closed": True,
        }

    # ---- 步骤 2: 多语句检查 ----
    valid_statements = [s for s in parsed if s is not None]
    if len(valid_statements) > 1:
        return False, f"不允许执行多条 SQL 语句（共 {len(valid_statements)} 条）", sql, {
            "statement_count": len(valid_statements),
        }

    if len(valid_statements) == 0:
        return False, "SQL 解析后无有效语句", "", None

    ast_root = valid_statements[0]

    # ---- 步骤 3: 裸表名自动补全（只补已知表名；补不了留给 table_refs 拒绝）----
    completed_tables = _complete_bare_tables(ast_root, allowed_schemas, known_tables)

    # ---- 步骤 4: 逐项校验（只保留有安全语义的规则）----
    checks = [
        ("statement_type", "语句类型", check_statement_type(ast_root)),
        ("dangerous_functions", "危险函数", check_dangerous_functions(ast_root)),
        ("cross_join", "CROSS JOIN", check_cross_join(ast_root)),
        ("table_refs", "表引用格式", check_table_refs(ast_root)),
    ]
    # 调用方给了 schema 白名单 → 在同一入口完成归属校验（防跨租户）。
    # 不传 allowed_schemas 的旧调用保持原行为（归属由调用方单独校验）。
    if allowed_schemas:
        checks.append(("table_ownership", "表归属", _check_ownership(ast_root, allowed_schemas)))

    ast_details: dict[str, Any] = {
        "rules": {},
        # 已删除规则：显式标 disabled（可观测：知道是"故意关闭"而不是"忘了跑"）
        "subquery_depth": "disabled",
        "aggregations": "disabled",
    }
    if completed_tables:
        ast_details["completed_tables"] = completed_tables

    for key, label, (passed, reason) in checks:
        ast_details["rules"][key] = {
            "label": label,
            "passed": passed,
            "reason": reason if not passed else None,
        }
        if not passed:
            ast_details["failed_rule"] = key
            ast_details["failed_reason"] = reason
            return False, f"安全拦截（{label}）: {reason}", sql, ast_details

    # 已删除规则的 rules 视图（统一 passed=True + disabled 标记）
    ast_details["rules"]["subquery_depth"] = {
        "label": "子查询嵌套深度", "passed": True, "disabled": True,
    }
    ast_details["rules"]["aggregations"] = {
        "label": "聚合函数白名单", "passed": True, "disabled": True,
    }

    # SELECT *：不再拦截，只记 warning（成本风险由超时/内存上限兜底）
    star_found = _has_bare_star(ast_root)
    ast_details["select_star"] = {"blocked": False}
    ast_details["rules"]["select_star"] = {"label": "SELECT *", "passed": True, "blocked": False}
    if star_found:
        ast_details["select_star"]["warning"] = (
            "SELECT * 会读取整行；成本风险由超时/内存上限兜底，不再是安全拦截"
        )
        ast_details["rules"]["select_star"]["warning"] = ast_details["select_star"]["warning"]
        logger.info("[ast_full_check] SELECT * 放行（仅告警）: %s", sql[:200])

    # ---- 步骤 5: 注入 LIMIT ----
    # 有裸表名补全时按修改后的 AST 重新渲染；否则保持原始文本（最小变更）。
    sql_for_output = ast_root.sql(dialect="duckdb") if completed_tables else sql
    sanitized_sql = inject_limit(sql_for_output, ast_root)

    # 补充元信息
    ast_details["statement_type"] = type(ast_root).__name__
    ast_details["has_limit"] = ast_root.find(exp.Limit) is not None

    return True, "", sanitized_sql, ast_details


# ==================== 内部辅助函数 ====================


def _strip_comments(sql: str) -> str:
    """去除 SQL 中的注释（单行注释 -- 和多行注释 /* */）。

    Args:
        sql: 原始 SQL 字符串。

    Returns:
        去除注释后的 SQL 字符串。
    """
    # 去除多行注释 /* ... */
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    # 去除单行注释 -- ...（需处理字符串中的 --）
    # 先处理字符串字面量外的 -- 注释
    result = []
    i = 0
    in_string = False
    string_char = None
    while i < len(sql):
        c = sql[i]
        if in_string:
            result.append(c)
            if c == string_char and (i == 0 or sql[i - 1] != "\\"):
                in_string = False
        elif c in ("'", '"'):
            in_string = True
            string_char = c
            result.append(c)
        elif c == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            # 找到 -- 注释，跳过到行尾
            i += 2
            while i < len(sql) and sql[i] not in ("\n", "\r"):
                i += 1
            # 保留换行符
            if i < len(sql):
                result.append(sql[i])
            i += 1
            continue
        else:
            result.append(c)
        i += 1
    return "".join(result)


def _remove_string_literals(sql: str) -> str:
    """移除 SQL 中的字符串字面量内容，用于在字符串外部进行关键字匹配。

    Args:
        sql: 原始 SQL 字符串。

    Returns:
        移除字符串字面量后的 SQL。
    """
    # 替换单引号和双引号字符串为占位符
    result = re.sub(r"'[^']*'", "''", sql)
    result = re.sub(r'"[^"]*"', '""', result)
    return result


def _is_valid_identifier(name: str) -> bool:
    """检查是否为合法的 SQL 标识符。

    Args:
        name: 待检查的标识符字符串。

    Returns:
        是否为合法标识符。
    """
    if not name:
        return False
    # 去掉可能的引号
    name = name.strip('"').strip("`")
    if not name:
        return False
    # 允许 Unicode 字母（中文 schema 名由 get_schema_name 从数据源名生成，属正常形态）。
    # 仍硬禁：引号（破坏引用闭合）、分号（多语句）、空白与注释符（拼接注入）。
    if any(ch in name for ch in ('"', "`", ";", "--", "/*", "*/")) or re.search(r"\s", name):
        return False
    # 标识符不能以数字开头（SQL 语义；"123invalid" 是字面量式名称，拒绝）
    if name[0].isdigit():
        return False
    return bool(re.match(r"^[\w\u4e00-\u9fff]+$", name))
