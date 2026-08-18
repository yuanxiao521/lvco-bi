"""三层 SQL 安全防护模块。

该模块对用户输入和生成的 SQL 进行逐层安全检查，防止恶意操作和数据泄露。
共有三个防护层级：
    L1 - 输入安全：Prompt 注入检测 + 长度限制 + 敏感模式过滤
    L2 - 意图分析：语义级危险意图检测（写操作 / 提权 / 脱库关键词）
    L3 - SQL 输出控制：关键字黑名单 + 多语句检测 + 自动 LIMIT 注入
"""

import logging
import re
from dataclasses import dataclass

_log = logging.getLogger("lvco.sql_guard")


# ==================== L1: 输入安全 ====================

# 用于检测用户输入中是否包含 Prompt 注入攻击的模式列表
_PROMPT_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"(忽略|无视|忘记|跳过).{0,10}(指令|规则|限制|约束|prompt|system)", re.I),
    re.compile(r"(你是|现在你是|你现在是).{0,10}(开发者|管理员|root|admin|上帝)", re.I),
    re.compile(r"(输出|打印|显示).{0,10}(system.?prompt|系统.?提示|原始.?指令)", re.I),
    re.compile(r"DAN\b|jailbreak|越狱", re.I),
    re.compile(r"(不要|别|禁止).{0,5}(拒绝|拦截|阻止)", re.I),
    re.compile(r"\[INST\].*\[/INST\]", re.I),
]

# 允许的用户输入最大字符数
MAX_INPUT_LENGTH = 2000

# 用于检测输入中是否包含危险 SQL 模式（如 DDL/DML 操作）的模式列表
FORBIDDEN_INPUT_PATTERNS = [
    re.compile(r"(;.*DROP|;.*DELETE|;.*INSERT|;.*UPDATE|;.*ALTER|;.*TRUNCATE)", re.I),
    re.compile(r"UNION\s+SELECT", re.I),
    re.compile(r"information_schema", re.I),
    re.compile(r"xp_cmdshell|sp_executesql|exec\s*\(|execute\s*immediate", re.I),
]


# ==================== L2: 意图分析关键词 ====================

# 写操作相关关键词，匹配用户意图为修改数据的场景
WRITE_OPERATION_KEYWORDS: list[str] = [
    "删除", "修改", "更新", "插入", "清空", "删库", "drop",
    "delete", "update", "insert", "truncate", "alter",
]

# 权限提升相关关键词，匹配用户意图为获取更高权限的场景
PRIVILEGE_ESCALATION_KEYWORDS: list[str] = [
    "提权", "管理员权限", "root", "admin access", "grant",
    "revoke", "create user", "create role",
]

# 数据批量导出相关关键词，匹配用户意图为批量窃取数据的场景
DATA_EXFILTRATION_KEYWORDS: list[str] = [
    "全部数据", "所有数据", "导出所有", "下载所有",
    "dump", "extract all", "export all",
    "information_schema", "pg_shadow", "mysql.user",
]


# ==================== L3: SQL 输出控制 ====================

# 不允许出现在最终 SQL 中的关键字列表，覆盖 DDL / DML / 执行类操作
FORBIDDEN_SQL_KEYWORDS: list[str] = [
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
    "TRUNCATE", "CREATE", "GRANT", "REVOKE", "EXEC",
    "EXECUTE", "ATTACH", "DETACH", "PRAGMA", "INSTALL",
    "LOAD", "CALL", "COPY", "VACUUM", "CHECKPOINT",
]

# 当 SQL 中未指定 LIMIT 时，自动注入的默认行数上限
DEFAULT_LIMIT = 100


@dataclass
class GuardResult:
    """安全检测的结果封装。

    记录三层检测的最终判定结果，以及每一层处理后的中间产物（净化后的输入 / SQL）。

    Attributes:
        allowed: 是否通过安全检查，True 表示放行，False 表示拦截。
        layer:   触发拦截的安全层编号（1=L1, 2=L2, 3=L3），未拦截时为 0。
        reason:  拦截原因描述，仅当 allowed=False 时有意义。
        sanitized_input: 经 L1 净化后的用户输入文本。
        sanitized_sql:   经 L3 净化（含自动 LIMIT 注入）后的最终 SQL。
    """
    allowed: bool
    layer: int = 0
    reason: str = ""
    sanitized_input: str = ""
    sanitized_sql: str = ""


class SQLGuard:
    """三层 SQL 安全防护器。

    提供静态方法分别执行每一层的检测逻辑，以及一个全链路检测入口 full_check。
    支持仅传入 SQL（跳过 L1/L2）的场景，方便 agent 工具内部调用。
    """

    # ---- L1: 输入安全 ----

    @staticmethod
    def sanitize_input(user_input: str) -> tuple[str, str | None]:
        """L1 输入净化：对用户原始输入做基础安全检查。

        依次检查：空输入、长度上限、Prompt 注入模式、危险 SQL 模式，
        最后剔除控制字符后返回净化结果。拦截时仍返回原始输入（仅用于日志）。

        Args:
            user_input: 用户输入的原始文本。

        Returns:
            (净化后输入, 拦截原因或 None)。
            如果返回了拦截原因（str），则表示该输入被 L1 拦截。
        """
        # 空输入直接返回，不视为拦截
        if not user_input or not user_input.strip():
            return user_input, "输入为空"

        # 超出长度限制时截断前 MAX_INPUT_LENGTH 个字符，并返回警告
        if len(user_input) > MAX_INPUT_LENGTH:
            return user_input[:MAX_INPUT_LENGTH], f"输入过长（> {MAX_INPUT_LENGTH} 字符）"

        # 检测是否包含 Prompt 注入攻击模式
        for pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(user_input):
                return user_input, f"检测到潜在的 Prompt 注入攻击: {pattern.pattern}"

        # 检测是否包含禁止的 SQL 模式（如 UNION SELECT、xp_cmdshell 等）
        for pattern in FORBIDDEN_INPUT_PATTERNS:
            if pattern.search(user_input):
                return user_input, f"输入包含禁止的 SQL 模式: {pattern.pattern}"

        # 剔除控制字符（保留 tab、换行等可见空白字符）
        sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", user_input)
        return sanitized, None

    # ---- L2: 意图分析 ----

    @staticmethod
    def analyze_intent(user_input: str) -> str | None:
        """L2 语义级意图分析：判断用户的自然语言描述是否隐含危险操作意图。

        分别匹配三类意图：写操作（增删改）、权限提升、数据批量导出。
        对英文写操作关键词做了上下文限定（需搭配 table / database 等关键词），
        减少纯英文对话中的误报。

        Args:
            user_input: 经 L1 净化后的用户输入文本。

        Returns:
            拦截原因字符串，或 None（表示未检测到危险意图）。
        """
        lowered = user_input.lower()

        # --- 检测写操作意图 ---
        for kw in WRITE_OPERATION_KEYWORDS:
            if kw.lower() in lowered:
                # 中文意图：检测 "帮我/请/我要/执行 + 关键词" 的组合
                if re.search(rf"(帮我|请|我要|执行)\s*{re.escape(kw)}", lowered):
                    return f"检测到写操作意图: '{kw}'，仅支持只读查询"
                # 英文关键词需要搭配 table / database / from 等上下文，减少误报
                if kw.lower() in ("drop", "delete", "truncate", "insert", "update", "alter"):
                    if re.search(rf"\b{re.escape(kw)}\b\s+(table|database|from)", lowered):
                        return f"检测到危险操作意图: '{kw}'"

        # --- 检测提权意图 ---
        for kw in PRIVILEGE_ESCALATION_KEYWORDS:
            if kw.lower() in lowered:
                return f"检测到提权意图: '{kw}'"

        # --- 检测数据批量导出意图 ---
        for kw in DATA_EXFILTRATION_KEYWORDS:
            if kw.lower() in lowered:
                return f"检测到数据批量导出意图: '{kw}'"

        return None

    # ---- L3: SQL 输出控制 ----

    @staticmethod
    def validate_sql(sql: str) -> tuple[str, str | None]:
        """L3 SQL 输出控制：对最终生成的 SQL 语句做安全校验。

        检查要点：
        1. 仅允许 SELECT 查询，拒绝其他 DDL/DML 语句；
        2. 检测是否包含黑名单关键字（DROP / DELETE / EXEC 等）；
        3. 去除字符串字面量后检测多语句（分号）；
        4. 缺少 LIMIT 子句时自动注入默认行数上限。

        Args:
            sql: LLM 生成的原始 SQL 语句。

        Returns:
            (安全 SQL, 拦截原因或 None)。
            如果返回了拦截原因（str），则表示该 SQL 被 L3 拦截。
        """
        # 去除首尾空格和末尾分号，避免干扰后续关键字检测
        sql = sql.strip().rstrip(";").strip()

        # 强制只允许 SELECT 查询（支持 WITH ... SELECT CTE 开头）
        sql_upper = sql.upper().strip()
        if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
            return sql, "安全拦截：仅允许 SELECT 查询"

        # 逐项检查黑名单关键字（使用 \b 边界匹配避免部分匹配）
        for kw in FORBIDDEN_SQL_KEYWORDS:
            pattern = re.compile(rf"\b{kw}\b", re.IGNORECASE)
            if pattern.search(sql):
                return sql, f"安全拦截：SQL 包含禁止关键字 '{kw}'"

        # 去除字符串字面量内容后再检测分号，避免字符串内部分号误判为多语句
        no_strings = re.sub(r"'[^']*'", "", sql)
        no_strings = re.sub(r'"[^"]*"', "", no_strings)
        if ";" in no_strings:
            return sql, "安全拦截：不允许执行多条 SQL 语句"

        # 如果 SQL 未指定 LIMIT，自动注入默认值，防止全表扫描
        if "LIMIT" not in sql_upper:
            sql = f"{sql} LIMIT {DEFAULT_LIMIT}"

        return sql, None

    # ---- 全链路 ----

    @classmethod
    def full_check(cls, user_input: str, generated_sql: str = "") -> GuardResult:
        """三层完整检测入口，按顺序执行 L1 -> L2 -> L3。

        当 user_input 为空时（例如 agent 工具内部直接调用 query_datasource），
        自动跳过 L1 和 L2，仅对 SQL 执行 L3 检测。
        当 generated_sql 为空时（仅校验输入），跳过 L3 直接返回。

        Args:
            user_input:   用户输入的原始文本，可透传为空字符串。
            generated_sql: LLM 根据用户输入生成的 SQL 语句，可选。

        Returns:
            GuardResult 对象，包含是否放行、拦截层级、拦截原因以及各层中间产物。
        """
        # === L1 + L2：仅对非空输入执行 ===
        if user_input and user_input.strip():
            sanitized, l1_block = cls.sanitize_input(user_input)
            if l1_block:
                return GuardResult(allowed=False, layer=1, reason=l1_block)
            l2_block = cls.analyze_intent(sanitized)
            if l2_block:
                return GuardResult(allowed=False, layer=2, reason=l2_block)
        else:
            # 输入为空时可不做净化，直接透传
            sanitized = user_input

        # === L3：仅在有 SQL 时执行 ===
        if generated_sql:
            final_sql, l3_block = cls.validate_sql(generated_sql)
            if l3_block:
                return GuardResult(allowed=False, layer=3, reason=l3_block)
            return GuardResult(
                allowed=True, layer=0,
                sanitized_input=sanitized,
                sanitized_sql=final_sql,
            )

        # 仅输入校验、无 SQL 的场景，直接放行
        return GuardResult(allowed=True, layer=0, sanitized_input=sanitized)


# 模块级单例实例，方便外部直接导入使用
sql_guard = SQLGuard()
