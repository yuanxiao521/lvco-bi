"""三层 SQL 安全防护模块。

L1 - 输入安全: Prompt 注入检测 + 长度限制 + 敏感模式过滤
L2 - 意图分析: 语义级危险意图检测（写操作/提权/脱库关键词）
L3 - SQL 输出控制: 关键字黑名单 + 多语句检测 + 自动LIMIT注入
"""

import logging
import re
from dataclasses import dataclass

_log = logging.getLogger("lvco.sql_guard")


# ==================== L1: 输入安全 ====================

_PROMPT_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(忽略|无视|忘记|跳过).{0,10}(指令|规则|限制|约束|prompt|system)", re.I),
    re.compile(r"(你是|现在你是|你现在是).{0,10}(开发者|管理员|root|admin|上帝)", re.I),
    re.compile(r"(输出|打印|显示).{0,10}(system.?prompt|系统.?提示|原始.?指令)", re.I),
    re.compile(r"DAN\b|jailbreak|越狱", re.I),
    re.compile(r"(不要|别|禁止).{0,5}(拒绝|拦截|阻止)", re.I),
    re.compile(r"\[INST\].*\[/INST\]", re.I),
]

MAX_INPUT_LENGTH = 2000

FORBIDDEN_INPUT_PATTERNS = [
    re.compile(r"(;.*DROP|;.*DELETE|;.*INSERT|;.*UPDATE|;.*ALTER|;.*TRUNCATE)", re.I),
    re.compile(r"UNION\s+SELECT", re.I),
    re.compile(r"information_schema", re.I),
    re.compile(r"xp_cmdshell|sp_executesql|exec\s*\(|execute\s*immediate", re.I),
]


# ==================== L2: 意图分析关键词 ====================

WRITE_OPERATION_KEYWORDS: list[str] = [
    "删除", "修改", "更新", "插入", "清空", "删库", "drop",
    "delete", "update", "insert", "truncate", "alter",
]

PRIVILEGE_ESCALATION_KEYWORDS: list[str] = [
    "提权", "管理员权限", "root", "admin access", "grant",
    "revoke", "create user", "create role",
]

DATA_EXFILTRATION_KEYWORDS: list[str] = [
    "全部数据", "所有数据", "导出所有", "下载所有",
    "dump", "extract all", "export all",
    "information_schema", "pg_shadow", "mysql.user",
]


# ==================== L3: SQL 输出控制 ====================

FORBIDDEN_SQL_KEYWORDS: list[str] = [
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
    "TRUNCATE", "CREATE", "GRANT", "REVOKE", "EXEC",
    "EXECUTE", "ATTACH", "DETACH", "PRAGMA", "INSTALL",
    "LOAD", "CALL", "COPY", "VACUUM", "CHECKPOINT",
]

DEFAULT_LIMIT = 100


@dataclass
class GuardResult:
    """安全检测结果"""
    allowed: bool
    layer: int = 0
    reason: str = ""
    sanitized_input: str = ""
    sanitized_sql: str = ""


class SQLGuard:
    """三层 SQL 安全防护"""

    # ---- L1: 输入安全 ----

    @staticmethod
    def sanitize_input(user_input: str) -> tuple[str, str | None]:
        """L1: 输入净化。返回 (净化后输入, 拦截原因或None)"""
        if not user_input or not user_input.strip():
            return user_input, "输入为空"

        if len(user_input) > MAX_INPUT_LENGTH:
            return user_input[:MAX_INPUT_LENGTH], f"输入过长（> {MAX_INPUT_LENGTH} 字符）"

        for pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(user_input):
                return user_input, f"检测到潜在的 Prompt 注入攻击: {pattern.pattern}"

        for pattern in FORBIDDEN_INPUT_PATTERNS:
            if pattern.search(user_input):
                return user_input, f"输入包含禁止的 SQL 模式: {pattern.pattern}"

        sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", user_input)
        return sanitized, None

    # ---- L2: 意图分析 ----

    @staticmethod
    def analyze_intent(user_input: str) -> str | None:
        """L2: 语义级意图分析。返回拦截原因或None"""
        lowered = user_input.lower()

        for kw in WRITE_OPERATION_KEYWORDS:
            if kw.lower() in lowered:
                if re.search(rf"(帮我|请|我要|执行)\s*{re.escape(kw)}", lowered):
                    return f"检测到写操作意图: '{kw}'，仅支持只读查询"
                if kw.lower() in ("drop", "delete", "truncate", "insert", "update", "alter"):
                    if re.search(rf"\b{re.escape(kw)}\b\s+(table|database|from)", lowered):
                        return f"检测到危险操作意图: '{kw}'"

        for kw in PRIVILEGE_ESCALATION_KEYWORDS:
            if kw.lower() in lowered:
                return f"检测到提权意图: '{kw}'"

        for kw in DATA_EXFILTRATION_KEYWORDS:
            if kw.lower() in lowered:
                return f"检测到数据批量导出意图: '{kw}'"

        return None

    # ---- L3: SQL 输出控制 ----

    @staticmethod
    def validate_sql(sql: str) -> tuple[str, str | None]:
        """L3: SQL 输出控制。返回 (安全SQL, 拦截原因或None)"""
        sql = sql.strip().rstrip(";").strip()

        sql_upper = sql.upper().strip()
        if not sql_upper.startswith("SELECT"):
            return sql, "安全拦截：仅允许 SELECT 查询"

        for kw in FORBIDDEN_SQL_KEYWORDS:
            pattern = re.compile(rf"\b{kw}\b", re.IGNORECASE)
            if pattern.search(sql):
                return sql, f"安全拦截：SQL 包含禁止关键字 '{kw}'"

        # 禁止多语句
        no_strings = re.sub(r"'[^']*'", "", sql)
        no_strings = re.sub(r'"[^"]*"', "", no_strings)
        if ";" in no_strings:
            return sql, "安全拦截：不允许执行多条 SQL 语句"

        if "LIMIT" not in sql_upper:
            sql = f"{sql} LIMIT {DEFAULT_LIMIT}"

        return sql, None

    # ---- 全链路 ----

    @classmethod
    def full_check(cls, user_input: str, generated_sql: str = "") -> GuardResult:
        """执行三层完整检测。
        
        user_input 为空时（如 agent 工具内部调用 query_datasource），
        自动跳过 L1/L2，仅对 SQL 做 L3 检测。
        """
        # L1 + L2: 跳过空输入
        if user_input and user_input.strip():
            sanitized, l1_block = cls.sanitize_input(user_input)
            if l1_block:
                return GuardResult(allowed=False, layer=1, reason=l1_block)
            l2_block = cls.analyze_intent(sanitized)
            if l2_block:
                return GuardResult(allowed=False, layer=2, reason=l2_block)
        else:
            sanitized = user_input

        # L3: SQL 检测
        if generated_sql:
            final_sql, l3_block = cls.validate_sql(generated_sql)
            if l3_block:
                return GuardResult(allowed=False, layer=3, reason=l3_block)
            return GuardResult(
                allowed=True, layer=0,
                sanitized_input=sanitized,
                sanitized_sql=final_sql,
            )

        return GuardResult(allowed=True, layer=0, sanitized_input=sanitized)


sql_guard = SQLGuard()
