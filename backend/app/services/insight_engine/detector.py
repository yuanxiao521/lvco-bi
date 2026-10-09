"""统计异常检测器 - z-score / 环比 / 同比 / 移动平均偏离"""

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class AnomalyType(str, Enum):
    z_score = "z_score"
    wow = "wow"           # 环比（周环比）
    yoy = "yoy"           # 同比（年同比）
    moving_average = "moving_average"


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


@dataclass
class TimePoint:
    """时间序列中的一个数据点"""
    timestamp: datetime
    values: dict[str, float]  # measure_field -> value


@dataclass
class Anomaly:
    """检测到的异常"""
    type: AnomalyType
    field: str
    severity: Severity
    current_value: float
    expected_value: float
    deviation: float  # 偏差比例 (current - expected) / expected
    direction: str    # "up" | "down"
    description: str


@dataclass
class Threshold:
    """检测阈值配置"""
    z_score: float = 2.5           # z-score 绝对值阈值
    wow_change: float = 0.20       # 环比变化率阈值 (20%)
    yoy_change: float = 0.30       # 同比变化率阈值 (30%)
    ma_window: int = 7             # 移动平均窗口（天）
    ma_deviation: float = 0.15     # 移动平均偏离阈值 (15%)
    min_history: int = 7           # 最少历史数据点数


def _safe_mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _safe_stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def _pct_change(current: float, previous: float) -> float:
    """计算变化率，previous 为 0 时返回 0"""
    if previous == 0:
        return 0.0
    return (current - previous) / abs(previous)


def _severity_from_deviation(deviation: float, threshold: float) -> Severity:
    """根据偏差程度确定严重性"""
    ratio = abs(deviation) / threshold if threshold > 0 else 0
    if ratio >= 2.0:
        return Severity.critical
    if ratio >= 1.0:
        return Severity.warning
    return Severity.info


def detect_z_score(
    series: list[TimePoint],
    field: str,
    threshold: Threshold,
) -> Anomaly | None:
    """z-score 检测：最新值偏离历史均值多少个标准差"""
    if len(series) < threshold.min_history + 1:
        return None
    history = [p.values.get(field, 0.0) for p in series[:-1]]
    current = series[-1].values.get(field, 0.0)
    mean = _safe_mean(history)
    stdev = _safe_stdev(history)
    if stdev == 0:
        return None
    z = (current - mean) / stdev
    if abs(z) < threshold.z_score:
        return None
    deviation = _pct_change(current, mean)
    return Anomaly(
        type=AnomalyType.z_score,
        field=field,
        severity=_severity_from_deviation(abs(z), threshold.z_score),
        current_value=current,
        expected_value=mean,
        deviation=deviation,
        direction="up" if current > mean else "down",
        description=f"z-score={z:.2f}（阈值 {threshold.z_score}），{field} 偏离均值 {deviation:+.1%}",
    )


def detect_wow(
    series: list[TimePoint],
    field: str,
    threshold: Threshold,
) -> Anomaly | None:
    """环比检测：最新值 vs 7天前的值"""
    if len(series) < 8:  # 至少 8 天数据才能算环比
        return None
    current = series[-1].values.get(field, 0.0)
    previous = series[-8].values.get(field, 0.0)
    change = _pct_change(current, previous)
    if abs(change) < threshold.wow_change:
        return None
    return Anomaly(
        type=AnomalyType.wow,
        field=field,
        severity=_severity_from_deviation(change, threshold.wow_change),
        current_value=current,
        expected_value=previous,
        deviation=change,
        direction="up" if current > previous else "down",
        description=f"环比变化 {change:+.1%}（阈值 {threshold.wow_change:.0%}），{field} 较上周{'上升' if change > 0 else '下降'}",
    )


def detect_yoy(
    series: list[TimePoint],
    field: str,
    threshold: Threshold,
) -> Anomaly | None:
    """同比检测：最新值 vs 去年同期值"""
    if len(series) < 365:
        return None
    current = series[-1].values.get(field, 0.0)
    previous = series[-365].values.get(field, 0.0)
    change = _pct_change(current, previous)
    if abs(change) < threshold.yoy_change:
        return None
    return Anomaly(
        type=AnomalyType.yoy,
        field=field,
        severity=_severity_from_deviation(change, threshold.yoy_change),
        current_value=current,
        expected_value=previous,
        deviation=change,
        direction="up" if current > previous else "down",
        description=f"同比变化 {change:+.1%}（阈值 {threshold.yoy_change:.0%}），{field} 较去年同期{'上升' if change > 0 else '下降'}",
    )


def detect_moving_average(
    series: list[TimePoint],
    field: str,
    threshold: Threshold,
) -> Anomaly | None:
    """移动平均偏离检测：最新值 vs 最近 N 天移动平均"""
    window = threshold.ma_window
    if len(series) < window + 1:
        return None
    recent = [p.values.get(field, 0.0) for p in series[-(window + 1):-1]]
    ma = _safe_mean(recent)
    current = series[-1].values.get(field, 0.0)
    if ma == 0:
        return None
    deviation = _pct_change(current, ma)
    if abs(deviation) < threshold.ma_deviation:
        return None
    return Anomaly(
        type=AnomalyType.moving_average,
        field=field,
        severity=_severity_from_deviation(deviation, threshold.ma_deviation),
        current_value=current,
        expected_value=ma,
        deviation=deviation,
        direction="up" if current > ma else "down",
        description=f"偏离{window}日均值 {deviation:+.1%}（阈值 {threshold.ma_deviation:.0%}），{field} {'高于' if deviation > 0 else '低于'}近期水平",
    )


def detect_anomalies(
    series: list[TimePoint],
    measure_fields: list[str],
    threshold: Threshold | dict | None = None,
) -> list[Anomaly]:
    """对时间序列执行所有异常检测，返回异常列表
    
    Args:
        series: 按时间升序排列的时间序列
        measure_fields: 要检测的度量字段列表
        threshold: 阈值配置（Threshold 对象或 dict，None 用默认值）
    """
    if threshold is None:
        t = Threshold()
    elif isinstance(threshold, dict):
        t = Threshold(**threshold)
    else:
        t = threshold

    if len(series) < t.min_history:
        return []

    anomalies: list[Anomaly] = []
    for field in measure_fields:
        for detector in (detect_z_score, detect_wow, detect_yoy, detect_moving_average):
            anomaly = detector(series, field, t)
            if anomaly is not None:
                anomalies.append(anomaly)
    return anomalies
