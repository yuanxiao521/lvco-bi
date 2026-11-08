# -*- coding: utf-8 -*-
"""画像统计的**共享口径**（唯一真源）。

背景：2026-11-08 有两条线同时需要"数据画像"——
- `agents/lead/lead_observe.profile_datasource`（给模型看的全表聚合，一次扫描）
- `services/datasource_profile.profile_datasource`（给 REST / 前端看的逐列画像，可采样）

两处各自实现了一套"类型判定 / 列名转义 / 空值率"逻辑，**口径漂移只是时间问题**
（一个四舍五入 4 位、一个 6 位；一个用 20 个类型标记、一个用 16 个）。
本模块把"统计语义"抽出来做唯一真源：两个消费方只保留**呈现形态**的差异。

约定（改动前先看这三条，它们会改变所有画像数字）：
1. `type_kind` 只看类型名里的**子串标记**（DuckDB 类型名可能带精度/时区，如
   `TIMESTAMP WITH TIME ZONE`、`DECIMAL(18,2)`），不做严格等值比较；
2. `null_rate` 的分母是**总行数**（不是非空行数），这样"空值率"才是"这列有多少比例不可用"；
3. 字符串/BLOB 列不给 MIN/MAX（字典序极值对分析无意义，给了反而误导）。
"""
from __future__ import annotations

from typing import Any

# 数值类型标记（DuckDB / 外部库映射后的类型名里包含即可）
NUMERIC_TYPE_MARKS: tuple[str, ...] = (
    "TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT",
    "FLOAT", "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
)
# 时间类型标记：注意 DATE 是 TIME 的子串，所以判断顺序必须"先时间后数值"无关，
# 但 temporal 内部要保证 TIMESTAMP 优先于 TIME（任一命中即可，此处仅做包含判断）。
TEMPORAL_TYPE_MARKS: tuple[str, ...] = ("DATE", "TIMESTAMP", "DATETIME", "TIME")


def norm_type(data_type: Any) -> str:
    """类型名规范化（大写去空白），供所有类型判定复用。"""
    return str(data_type or "").strip().upper()


def type_kind(data_type: Any) -> str:
    """把类型名归成 numeric / temporal / other（只用于选择聚合函数）。

    先判 temporal：`DATE` 本身包含 `TIME` 的子串，但两者都属于 temporal，无冲突；
    关键是**不能把 INTEGER 误判成 temporal**——INT 里没有这些标记，安全。
    """
    t = norm_type(data_type)
    if any(mark in t for mark in TEMPORAL_TYPE_MARKS):
        return "temporal"
    if any(mark in t for mark in NUMERIC_TYPE_MARKS):
        return "numeric"
    return "other"


def supports_range(data_type: Any) -> bool:
    """MIN/MAX 对"业务语义"是否有意义：数值/时间给范围，字符串不给。"""
    return type_kind(data_type) in ("numeric", "temporal")


def null_rate(total: Any, non_null: Any, *, digits: int = 4) -> float | None:
    """空值率 = (总行数 - 非空行数) / 总行数。

    分母用**总行数**：问的是"这列有多少比例不可用"，不是"非空里有多少空"。
    total 为 0/None 时返回 None（空表没有空值率，别返 0 迷惑人）。
    """
    try:
        total_i = int(total or 0)
        non_null_i = int(non_null or 0)
    except (TypeError, ValueError):
        return None
    if total_i <= 0:
        return None
    rate = (total_i - non_null_i) / total_i
    return round(max(0.0, min(1.0, rate)), digits)


def quote_ident(name: Any) -> str:
    """列名加双引号（内部双引号转义）。

    业务列名常带空格/中文/标点，不加引号会被解析器当语法；已经在引号里的不重复加。
    """
    text = str(name)
    return '"' + text.replace('"', '""') + '"'
