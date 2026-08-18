from datetime import datetime, timedelta

from app.services.insight_engine.detector import (
    Anomaly, AnomalyType, Severity, Threshold, TimePoint,
    detect_anomalies, detect_z_score, detect_wow, detect_moving_average,
    _pct_change, _severity_from_deviation,
)


def _make_series(values: list[float], field: str = "amount", start: str = "2026-01-01") -> list[TimePoint]:
    """辅助：从数值列表生成时间序列（每天一个点）"""
    base = datetime.fromisoformat(start)
    return [
        TimePoint(timestamp=base + timedelta(days=i), values={field: v})
        for i, v in enumerate(values)
    ]


def test_pct_change_basic():
    assert _pct_change(120, 100) == 0.2
    assert _pct_change(80, 100) == -0.2
    assert _pct_change(100, 0) == 0.0


def test_severity_from_deviation():
    assert _severity_from_deviation(0.5, 0.2) == Severity.critical  # 2.5x threshold
    assert _severity_from_deviation(0.25, 0.2) == Severity.warning  # 1.25x
    assert _severity_from_deviation(0.1, 0.2) == Severity.info      # below threshold


def test_detect_z_score_finds_anomaly():
    """最后一个值远超均值时应检测到 z-score 异常"""
    values = [100, 102, 98, 101, 99, 103, 100, 250]
    series = _make_series(values)
    anomaly = detect_z_score(series, "amount", Threshold())
    assert anomaly is not None
    assert anomaly.type == AnomalyType.z_score
    assert anomaly.direction == "up"
    assert anomaly.current_value == 250


def test_detect_z_score_no_anomaly():
    """正常波动不应触发"""
    values = [100, 102, 98, 101, 99, 103, 100, 101]
    series = _make_series(values)
    anomaly = detect_z_score(series, "amount", Threshold())
    assert anomaly is None


def test_detect_z_score_insufficient_history():
    """历史数据不足不应检测"""
    values = [100, 102]
    series = _make_series(values)
    anomaly = detect_z_score(series, "amount", Threshold())
    assert anomaly is None


def test_detect_wow_finds_anomaly():
    """环比变化超过阈值应检测到"""
    # 8天数据，最后一天比7天前涨了 50%
    values = [100, 100, 100, 100, 100, 100, 100, 150]
    series = _make_series(values)
    anomaly = detect_wow(series, "amount", Threshold())
    assert anomaly is not None
    assert anomaly.type == AnomalyType.wow
    assert anomaly.direction == "up"


def test_detect_wow_no_anomaly():
    """环比变化不大不应触发"""
    values = [100, 100, 100, 100, 100, 100, 100, 110]
    series = _make_series(values)
    anomaly = detect_wow(series, "amount", Threshold())
    assert anomaly is None  # 10% < 20% threshold


def test_detect_moving_average_finds_anomaly():
    """偏离移动平均应检测到"""
    values = [100, 100, 100, 100, 100, 100, 100, 130]
    series = _make_series(values)
    anomaly = detect_moving_average(series, "amount", Threshold())
    assert anomaly is not None
    assert anomaly.type == AnomalyType.moving_average
    assert anomaly.direction == "up"


def test_detect_moving_average_no_anomaly():
    """正常波动不应触发"""
    values = [100, 102, 98, 101, 99, 103, 100, 101]
    series = _make_series(values)
    anomaly = detect_moving_average(series, "amount", Threshold())
    assert anomaly is None


def test_detect_anomalies_multiple_fields():
    """多字段应分别检测"""
    base = datetime(2026, 1, 1)
    series = [
        TimePoint(timestamp=base + timedelta(days=i), values={"amount": a, "count": c})
        for i, (a, c) in enumerate(
            [(100, 10), (102, 11), (98, 9), (101, 10), (99, 11), (103, 10), (100, 10), (250, 30)]
        )
    ]
    anomalies = detect_anomalies(series, ["amount", "count"], Threshold())
    # amount 和 count 都有 z_score 异常
    fields = {a.field for a in anomalies}
    assert "amount" in fields
    assert "count" in fields


def test_detect_anomalies_insufficient_history():
    """历史数据不足应返回空"""
    series = _make_series([100, 102])
    anomalies = detect_anomalies(series, ["amount"], Threshold())
    assert anomalies == []


def test_detect_anomalies_with_dict_threshold():
    """支持 dict 形式的 threshold"""
    values = [100, 100, 100, 100, 100, 100, 100, 150]
    series = _make_series(values)
    anomalies = detect_anomalies(series, ["amount"], {"z_score": 2.0, "wow_change": 0.1})
    assert len(anomalies) > 0


def test_detect_anomalies_downward():
    """下降也应被检测到"""
    values = [100, 100, 100, 100, 100, 100, 100, 50]
    series = _make_series(values)
    anomalies = detect_anomalies(series, ["amount"], Threshold())
    down_anomalies = [a for a in anomalies if a.direction == "down"]
    assert len(down_anomalies) > 0
