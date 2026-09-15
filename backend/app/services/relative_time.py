"""相对 / 自然语言时间过滤解析：把 LLM 产出的时间表达式解析为具体日期边界。

背景：用户问"最近3个月的销售额/今年/上月"时，LLM 常直接把表达式
（如 ``now() - interval 3 month``、``last 3 months``）塞进 query_engine 的
``filter.value``。DuckDB 对日期列做参数化比较时无法解析这类表达式，导致
Conversion Error，第一次查询直接失败、只能靠裸 SQL 兜底。

本模块把这些表达式解析为「具体起始/结束日期」，在工具边界（query_engine /
query_sql）把 value 替换为解析结果，让结构化查询一次成功，避免退化到裸 SQL。

心智模型：表达式声明的是「一段相对窗口」，解析后变成「绝对的 [start, end]」。
调用方按 op 决定用 start 还是 end：
- gte/gt  → start
- lte/lt  → end
- eq/between → 双端
"""
from __future__ import annotations

import re
from datetime import date, datetime

from dateutil.relativedelta import relativedelta

_UNITS: dict[str, str] = {
    "day": "days", "days": "days", "d": "days",
    "week": "weeks", "weeks": "weeks", "w": "weeks",
    "month": "months", "months": "months", "mon": "months",
    "year": "years", "years": "years", "y": "years",
    "quarter": "months", "quarters": "months",
    "hour": "hours", "hours": "hours",
    "minute": "minutes", "minutes": "minutes",
}

# 形如：now() - interval 3 month / now() - 30 day / CURRENT_DATE - INTERVAL '6 mon' / date_trunc('month', now())
_INTERVAL_RE = re.compile(
    r"^\s*(now\s*\(\s*\)|current_date|curdate|to_date\s*\(\s*'now'\s*\))\s*"
    r"([-+])\s*(?:interval\s+)?['\"]?\s*(\d+(?:\.\d+)?)\s*"
    r"([A-Za-z]+)\s*['\"]?\s*$",
    re.IGNORECASE,
)

# 形如：last 3 months / past 30 days / 最近6个月 / 过去一年 / 近7天
_LASTN_RE = re.compile(
    r"^\s*0*(?:last|past|recent|最近|过去|近|前)?\s*(\d+)\s*"
    r"(day|days|天|week|weeks|周|month|months|个月|month|year|years|年|year)?\s*$",
    re.IGNORECASE,
)

# 本月 / 今年 / 上月 / 去年 / 今天 / 昨天 / 本周
_SPECIAL: dict[str, tuple[relativedelta, relativedelta | None]] = {
    # 形如 now() - interval 3 month / now() - 30 day / CURRENT_DATE - INTERVAL '6 mon' / date_trunc('month', now())
    "昨天": (relativedelta(days=-1), None),
    "前天": (relativedelta(days=-2), None),
    "今天": (relativedelta(days=0), None),
}


def _normalize_unit(raw: str) -> str:
    return _UNITS.get(raw.lower(), "days")


def _apply_units(base: date, n: float, unit: str) -> date:
    """在 base 上减去/加上 n 个 unit（支持小数，向下取到天）。"""
    try:
        if unit == "months":
            whole = int(n)
            r = relativedelta(months=whole)
            rem = n - whole
            return base + r
        return base + relativedelta(days=int(n))
    except Exception:  # noqa: BLE001
        return base + relativedelta(days=int(n))


def resolve_window(value) -> dict | None:
    """把表达式解析为 {start, end} 窗口；非相对时间返回 None。

    - start：窗口起点（含），如「最近3个月」→ 3 个月前当天
    - end  ：窗口终点（含），默认今天
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    today = datetime.now().date()

    # 1) now() - interval N unit
    m = _INTERVAL_RE.match(v)
    if m:
        delta = -1 if m.group(2) == "-" else 1
        n = float(m.group(3)) * delta
        unit = _normalize_unit(m.group(4))
        start = _apply_units(today, n, unit)
        return {"start": start, "end": today}

    # 2) last/past/recent N unit
    m2 = _LASTN_RE.match(v)
    if m2 and m2.group(1):
        n = float(m2.group(1))
        unit_raw = m2.group(2) or "天"
        unit_map = {"天": "days", "日": "days", "周": "weeks", "星期": "weeks",
                    "月": "months", "个月": "months", "年": "years"}
        words = {"day": "days", "days": "days", "week": "weeks", "weeks": "weeks",
                 "month": "months", "months": "months", "year": "years", "years": "years"}
        unit = words.get(unit_raw.lower()) or unit_map.get(unit_raw, "days")
        delta = -1
        start = _apply_units(today, n * delta, unit)
        return {"start": start, "end": today}

    # 3) 特殊词
    if v in ("本月",):
        return {"start": date(today.year, today.month, 1), "end": today}
    if v == "上月":
        first = date(today.year, today.month, 1)
        prev_first = first - relativedelta(months=1)
        return {"start": prev_first, "end": first - relativedelta(days=1)}
    if v == "今年":
        return {"start": date(today.year, 1, 1), "end": today}
    if v == "去年":
        return {"start": date(today.year - 1, 1, 1), "end": date(today.year - 1, 12, 31)}
    if v in ("本周",):
        return {"start": today - relativedelta(days=today.weekday()), "end": today}
    if v == "昨天":
        return {"start": today - relativedelta(days=1), "end": today - relativedelta(days=1)}
    if v == "今天" or v == "今日":
        return {"start": today, "end": today}
    return None


def apply_to_filter(op: str, value) -> tuple[str, str]:
    """把过滤条件里的相对时间值替换为具体边界，返回 (op, resolved_value)。

    非相对时间 / 解析失败时原样返回。op 决定用窗口的 start 还是 end：
    - gte/gt → start
    - lte/lt → end
    - eq/between → 没解析出就保留原值（防止误伤数值过滤）
    """
    if not isinstance(value, str):
        return op, value
    w = resolve_window(value)
    if w is None:
        return op, value
    lop = (op or "eq").lower()
    if lop in ("gte", "gt", "ge"):
        return "gte", w["start"].isoformat()
    if lop in ("lte", "lt", "le"):
        return "lte", w["end"].isoformat()
    if lop == "eq":
        return "between", [w["start"].isoformat(), w["end"].isoformat()]
    return op, value


def apply_to_filters(filters: list) -> list:
    """批量改写过滤条件（dict / FilterConfig 兼容）。返回新 list。"""
    out: list = []
    for f in filters or []:
        if not isinstance(f, dict):
            out.append(f)
            continue
        f = dict(f)
        f["op"], f["value"] = apply_to_filter(str(f.get("op", "eq")), f.get("value"))
        out.append(f)
    return out