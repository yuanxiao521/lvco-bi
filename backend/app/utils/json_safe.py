"""把数据库取回的样例值转成 JSON 可序列化形态。

背景（真机踩过）：PostgreSQL 数据源的 schema 同步会为每列取 3 个样例值塞进
schema_meta（JSONB 列）。UUID / time / timedelta / bytea 等类型不是 JSON 原生类型，
旧实现只处理了 date/datetime/Decimal，于是任何带 UUID 主键的表（极其常见）都会
在 flush 时抛 "Object of type UUID is not JSON serializable"，整个同步 500、
数据源直接不可用。
"""
import enum
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal


def json_safe(value):
    """递归地把值转成可 JSON 序列化的 Python 对象。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        try:
            return float(value)
        except (ValueError, OverflowError):
            return str(value)
    if isinstance(value, (uuid.UUID,)):
        return str(value)
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.hex()[:64]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)
