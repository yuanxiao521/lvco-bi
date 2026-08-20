"""基于 AST 的 SQL 安全校验层（L4 - 语法树级防护）。

在 SQLGuard 的三层防护（输入安全、意图分析、关键字黑名单）之上，
增加第四层 AST 解析校验，通过 SQLGlot 将 SQL 解析为语法树后进行精准校验：

    1. 语句类型校验 —— 仅允许 SELECT / WITH...SELECT
    2. 子查询嵌套深度限制 —— 防止深度嵌套导致的性能问题
    3. UNION / EXCEPT / INTERSECT 检测 —— 防止集合操作绕过表名校验
    4. CROSS JOIN 检测 —— 防止笛卡尔积
    5. SELECT * 检测 —— 强制显式列名
    6. 聚合函数白名单 —— 仅允许预定义的聚合函数
    7. 危险函数黑名单 —— 阻止文件读写/系统调用等危险操作
    8. 表引用格式校验 —— 要求所有表引用使用 schema.data 格式
    9. 自动 LIMIT 注入 —— 缺失 LIMIT 时自动添加默认值
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger("lvco.sql_guard_ast")

# ==================== 常量定义 ====================

# 白名单聚合函数 —— 仅允许这些聚合函数出现在 SQL 中
ALLOWED_AGGREGATIONS = frozenset({
    "SUM", "AVG", "COUNT", "MAX", "MIN",
    "STDDEV", "MEDIAN", "COUNT_DISTINCT",
})

# 危险函数黑名单 —— 这些函数可能用于文件读写、系统调用或修改数据库状态
DANGEROUS_FUNCTIONS = frozenset({
    "READ_BLOB", "READ_TEXT", "COPY_DATABASE",
    "READ_CSV", "READ_PARQUET", "READ_JSON",
    "WRITE_CSV", "WRITE_PARQUET", "WRITE_JSON",
    "CALL", "LOAD", "INSTALL", "PRAGMA", "SET",
})

# 表引用必须匹配的正则: schema.data（schema 为标识符，data 为表名）
_TABLE_REF_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*$")

# 默认注入的 LIMIT 值
_DEFAULT_LIMIT = 100

# 最大子查询嵌套深度
_MAX_SUBQUERY_DEPTH = 2


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
    """校验 SQL 语句类型，仅允许 SELECT 或 WITH...SELECT。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    # WITH 语句：包含 CTE 定义，内部必须包含 SELECT
    if isinstance(ast, exp.With):
        # 检查 WITH 中是否包含 SELECT
        if ast.find(exp.Select) is None:
            return False, "仅允许 SELECT 查询：WITH 语句中未找到 SELECT"
        return True, ""

    # 普通 SELECT 语句
    if isinstance(ast, exp.Select):
        return True, ""

    # 其他语句类型（如 INSERT、CREATE、DROP 等）一律拦截
    return False, f"仅允许 SELECT 查询，当前语句类型: {type(ast).__name__}"


def check_subquery_depth(
    ast: exp.Expression,
    max_depth: int = _MAX_SUBQUERY_DEPTH,
) -> tuple[bool, str]:
    """检查子查询嵌套深度，防止过深的嵌套导致性能问题。

    通过 DFS 遍历 AST，遇到 Subquery 节点时深度 +1，
    如果当前深度超过 max_depth 则拦截。

    Args:
        ast: 解析后的 AST 根节点。
        max_depth: 允许的最大子查询嵌套深度，默认为 2。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    # 使用递归 DFS 遍历 AST，跟踪当前子查询嵌套深度
    def _dfs(node: exp.Expression, current_depth: int) -> bool:
        # 如果当前节点是子查询，深度 +1 并检查是否超限
        if isinstance(node, exp.Subquery):
            current_depth += 1
            if current_depth > max_depth:
                return False

        # 递归遍历所有子节点
        for arg_key, arg_value in node.args.items():
            if isinstance(arg_value, exp.Expression):
                if not _dfs(arg_value, current_depth):
                    return False
            elif isinstance(arg_value, list):
                for item in arg_value:
                    if isinstance(item, exp.Expression):
                        if not _dfs(item, current_depth):
                            return False
        return True

    if not _dfs(ast, 0):
        return False, f"子查询嵌套深度超过限制（最大 {max_depth} 层）"
    return True, ""


def check_union(ast: exp.Expression) -> tuple[bool, str]:
    """检测 SQL 中是否包含 UNION / EXCEPT / INTERSECT 集合操作。

    集合操作通常用于合并多个查询结果，可能被用于绕过表名校验，
    因此需要单独检测并拦截。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    unions = list(ast.find_all(exp.Union))
    if unions:
        return False, "不允许使用 UNION 操作"

    excepts = list(ast.find_all(exp.Except))
    if excepts:
        return False, "不允许使用 EXCEPT 操作"

    intersects = list(ast.find_all(exp.Intersect))
    if intersects:
        return False, "不允许使用 INTERSECT 操作"

    return True, ""


def check_cross_join(ast: exp.Expression) -> tuple[bool, str]:
    """检测 SQL 中是否包含 CROSS JOIN（笛卡尔积）。

    CROSS JOIN 会导致笛卡尔积，可能产生大量数据，消耗过多资源。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    for join_node in ast.find_all(exp.Join):
        # 检查 Join 节点的 kind 属性是否为 'CROSS'
        kind = join_node.args.get("kind")
        if kind and str(kind).upper() == "CROSS":
            return False, "不允许使用 CROSS JOIN（笛卡尔积）"
        # 也可以通过 method 属性判断
        method = join_node.args.get("method")
        if method and str(method).upper() == "CROSS":
            return False, "不允许使用 CROSS JOIN（笛卡尔积）"

    return True, ""


def check_select_star(ast: exp.Expression) -> tuple[bool, str]:
    """检测 SQL 中是否包含 SELECT *。

    要求所有查询必须显式指定列名，不允许使用通配符。
    这有助于控制返回的数据量，并避免意外暴露敏感列。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    # 查找所有 Star 节点（包括 SELECT * 和 table.*）
    stars = list(ast.find_all(exp.Star))
    if stars:
        return False, "不允许使用 SELECT *，请显式指定列名"
    return True, ""


def check_aggregations(ast: exp.Expression) -> tuple[bool, str]:
    """检查所有聚合函数是否在白名单 ALLOWED_AGGREGATIONS 中。

    同时检查 SQLGlot 已识别的聚合函数（AggFunc 子类）和
    未被识别的匿名函数（Anonymous），确保只使用允许的聚合函数。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    # 检查已识别的聚合函数（如 SUM、COUNT、AVG 等 SQLGlot 内置类型）
    for func in ast.find_all(exp.AggFunc):
        # 获取函数名：优先使用 sql_name()，其次使用类名
        if hasattr(func, "sql_name"):
            func_name = func.sql_name().upper()
        else:
            func_name = type(func).__name__.upper()
        if func_name not in ALLOWED_AGGREGATIONS:
            return False, f"聚合函数 '{func_name}' 不在白名单中"

    # 检查匿名函数（未被 SQLGlot 识别的函数调用）
    for func in ast.find_all(exp.Anonymous):
        func_name = func.name.upper() if hasattr(func, "name") else ""
        if not func_name:
            continue
        # 如果函数名是已知的聚合函数，必须通过白名单校验
        if func_name in ALLOWED_AGGREGATIONS:
            continue  # 在白名单中，放行
        # 如果函数名看起来像聚合函数但不在白名单中，拦截
        if _looks_like_aggregate(func_name):
            return False, f"聚合函数 '{func_name}' 不在白名单中"

    return True, ""


def check_dangerous_functions(ast: exp.Expression) -> tuple[bool, str]:
    """检查 SQL 中是否包含危险函数调用。

    危险函数包括文件读写（READ_CSV、WRITE_CSV 等）、
    系统调用（CALL、LOAD 等）和数据库配置修改（PRAGMA、SET 等）。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    # 检查已识别的函数
    for func in ast.find_all(exp.Func):
        if hasattr(func, "sql_name"):
            func_name = func.sql_name().upper()
        else:
            func_name = type(func).__name__.upper()
        if func_name in DANGEROUS_FUNCTIONS:
            return False, f"SQL 包含危险函数: '{func_name}'"

    # 检查匿名函数（未被 SQLGlot 识别的函数调用）
    for func in ast.find_all(exp.Anonymous):
        func_name = func.name.upper() if hasattr(func, "name") else ""
        if func_name in DANGEROUS_FUNCTIONS:
            return False, f"SQL 包含危险函数: '{func_name}'"

    return True, ""


def check_table_refs(ast: exp.Expression) -> tuple[bool, str]:
    """检查所有表引用是否匹配 schema.data 模式（两段式表名）。

    要求所有表引用必须带 schema 前缀，如 "my_schema"."data"，
    不允许裸表名（如 "data"），确保查询只访问已注册的数据源。

    Args:
        ast: 解析后的 AST 根节点。

    Returns:
        (是否通过, 拦截原因)。通过时 reason 为空字符串。
    """
    for table in ast.find_all(exp.Table):
        # 获取表的各个部分
        table_name = table.name  # 表名部分
        db = table.args.get("db")  # schema 部分

        if db is None:
            return False, (
                f"表引用 '{table.sql(dialect='duckdb')}' 缺少 schema 前缀，"
                f"格式应为 schema.data"
            )

        # 检查 schema 和表名是否都是合法的标识符
        db_name = db.name if hasattr(db, "name") else str(db)
        if not _is_valid_identifier(db_name) or not _is_valid_identifier(table_name):
            return False, (
                f"表引用 '{table.sql(dialect='duckdb')}' 包含非法标识符"
            )

    return True, ""


def inject_limit(sql: str, ast: exp.Expression) -> str:
    """检查 SQL 是否包含 LIMIT 子句，缺失时自动注入 LIMIT 100。

    Args:
        sql: 原始 SQL 字符串。
        ast: 解析后的 AST 根节点。

    Returns:
        处理后的 SQL 字符串（可能追加了 LIMIT 子句）。
    """
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


def ast_full_check(sql: str) -> tuple[bool, str, str, dict | None]:
    """AST 全量校验入口，执行完整的语法树安全检查 pipeline。

    校验流程：
    1. 解析 SQL 为 AST
    2. 检查是否为多语句（>1 条语句）
    3. 依次执行所有校验规则
    4. 自动注入 LIMIT
    5. 汇总结果

    Args:
        sql: 待校验的原始 SQL 字符串。

    Returns:
        (是否放行, 拦截原因, 净化后的 SQL, AST 详细信息)。
        放行时 sanitized_sql 为注入 LIMIT 后的 SQL，ast_details 包含校验结果摘要。
        拦截时 sanitized_sql 返回原始 SQL，ast_details 包含失败信息。
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
        return False, f"SQL 解析失败: {sql[:200]}", sql, {"parse_error": str(sql[:200])}

    # ---- 步骤 2: 多语句检查 ----
    # 过滤掉空语句（纯注释等）
    valid_statements = [s for s in parsed if s is not None]
    if len(valid_statements) > 1:
        return False, f"不允许执行多条 SQL 语句（共 {len(valid_statements)} 条）", sql, {
            "statement_count": len(valid_statements),
        }

    if len(valid_statements) == 0:
        return False, "SQL 解析后无有效语句", "", None

    ast_root = valid_statements[0]

    # ---- 步骤 3: 逐项校验 ----
    checks = [
        ("statement_type", "语句类型", check_statement_type(ast_root)),
        ("subquery_depth", "子查询嵌套深度", check_subquery_depth(ast_root)),
        ("union", "集合操作", check_union(ast_root)),
        ("cross_join", "CROSS JOIN", check_cross_join(ast_root)),
        ("select_star", "SELECT *", check_select_star(ast_root)),
        ("aggregations", "聚合函数", check_aggregations(ast_root)),
        ("dangerous_functions", "危险函数", check_dangerous_functions(ast_root)),
        ("table_refs", "表引用格式", check_table_refs(ast_root)),
    ]

    # 构建详细信息字典
    ast_details: dict[str, Any] = {
        "rules": {},
    }

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

    # ---- 步骤 4: 注入 LIMIT ----
    sanitized_sql = inject_limit(sql, ast_root)

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
    return bool(re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name))


def _looks_like_aggregate(func_name: str) -> bool:
    """判断函数名是否看起来像聚合函数。

    通过检查函数名是否包含常见的聚合函数关键词来判断。

    Args:
        func_name: 大写的函数名。

    Returns:
        是否看起来像聚合函数。
    """
    aggregate_keywords = {
        "SUM", "AVG", "COUNT", "MAX", "MIN",
        "STDDEV", "MEDIAN", "STDDEV_POP", "STDDEV_SAMP",
        "VAR_POP", "VAR_SAMP", "VARIANCE",
        "CORR", "COVAR_POP", "COVAR_SAMP",
        "PERCENTILE", "PERCENTILE_CONT", "PERCENTILE_DISC",
        "FIRST", "LAST", "LISTAGG", "STRING_AGG",
        "GROUP_CONCAT", "ARRAY_AGG", "BIT_AND", "BIT_OR",
        "BOOL_AND", "BOOL_OR", "EVERY", "SOME",
    }
    return func_name in aggregate_keywords