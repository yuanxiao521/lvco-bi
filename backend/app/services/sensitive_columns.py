"""敏感列识别与脱敏（列级治理）。

为什么需要
----------
连上客户的业务库后，schema 同步会为每列取 3 个样例值、预览会返回前 N 行，这两条
数据都会流向两个地方：数据库里的 schema_meta（可被 API 读）和 LLM 上下文（提示词）。
真实业务库里 password_hash / id_card / bank_card 这种列一旦进了 LLM 上下文，就等于
把 PII 发给了第三方模型——这是比"密码明文落库"更隐蔽的一类风险。

策略
----
1. 同步时按列名启发式识别敏感列（secrets/PII），标记 schema_meta.fields[i].sensitive，
   并**不采集样例值**；
2. 预览时把敏感列的值替换成 ***；
3. 工具层（list_fields）与查询层（LLM 生成的 SQL / 图表配置）都拒绝引用敏感列；
4. 用户可以手工改标记（前端字段编辑器里的"敏感"开关），手工值优先于启发式。
"""
import re

# 强敏感：无论业务是否需要，默认都不进样例、不可查
_SENSITIVE_NAME_PATTERNS = [
    re.compile(r"pass(word|wd|phrase)?", re.I),
    re.compile(r"pwd", re.I),
    re.compile(r"secret", re.I),
    re.compile(r"token", re.I),
    re.compile(r"api[_-]?key", re.I),
    re.compile(r"private[_-]?key", re.I),
    re.compile(r"credential", re.I),
    re.compile(r"cvv|cvc", re.I),
    re.compile(r"(id|identity)[_-]?card", re.I),
    re.compile(r"bank[_-]?card|card[_-]?no", re.I),
    re.compile(r"ssn", re.I),
    re.compile(r"phone|mobile", re.I),
    re.compile(r"密码|密钥|令牌|身份证|银行卡|手机号|手机号码", re.I),
]

# 弱敏感（可选开启）：邮箱/地址/薪资这类通常要参与分析，默认不拦
_WEAK_SENSITIVE_PATTERNS = [
    re.compile(r"e?mail", re.I),
    re.compile(r"address|地址", re.I),
    re.compile(r"salary|薪资|工资", re.I),
]

MASK = "***"


def detect_sensitive(name: str, *, include_weak: bool = False) -> str | None:
    """按列名判断是否敏感，敏感则返回原因（用于前端展示/审计）。"""
    if not name:
        return None
    patterns = list(_SENSITIVE_NAME_PATTERNS)
    if include_weak:
        patterns += _WEAK_SENSITIVE_PATTERNS
    for pattern in patterns:
        if pattern.search(str(name)):
            return f"列名命中敏感特征（{pattern.pattern}）"
    return None


def is_sensitive(field: dict) -> bool:
    return bool(field.get("sensitive"))


def sensitive_names(fields: list[dict] | None) -> set[str]:
    return {
        str(f.get("name"))
        for f in (fields or [])
        if isinstance(f, dict) and f.get("name") and f.get("sensitive")
    }


def mark_fields(fields: list[dict] | None, *, include_weak: bool = False) -> list[dict]:
    """给字段补 sensitive 标记；用户手工设置过（键存在）则不覆盖。"""
    out: list[dict] = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        item = dict(field)
        if "sensitive" not in item:
            reason = detect_sensitive(item.get("name", ""), include_weak=include_weak)
            item["sensitive"] = bool(reason)
            if reason:
                item["sensitive_reason"] = reason
        if item.get("sensitive"):
            # 敏感列不保留样例值：样例是"脱敏前"的原始数据，最容易泄漏
            item["sample"] = []
        out.append(item)
    return out


def mask_rows(
    columns: list[str], rows: list[list], sensitive: set[str]
) -> list[list]:
    """把行数据里敏感列的值替换成 ***（列名不区分大小写）。"""
    if not rows or not sensitive:
        return rows
    lowered = {c.lower() for c in sensitive}
    idx = [i for i, c in enumerate(columns) if str(c).lower() in lowered]
    if not idx:
        return rows
    masked: list[list] = []
    for row in rows:
        values = list(row)
        for i in idx:
            if i < len(values) and values[i] is not None:
                values[i] = MASK
        masked.append(values)
    return masked


def referenced_sensitive(sql: str, sensitive: set[str]) -> list[str]:
    """检查 SQL 是否引用了敏感列（标识符边界匹配，忽略引号/大小写）。"""
    if not sql or not sensitive:
        return []
    hits: list[str] = []
    for name in sensitive:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", re.I
        )
        if pattern.search(sql):
            hits.append(name)
    return hits
