from __future__ import annotations

import pytest

from app.services.metric_parser import (
    InvalidFormulaError,
    MetricRef,
    detect_cycle,
    parse_derived_formula,
    validate_formula_syntax,
)


def test_empty_formula_returns_empty_list():
    assert parse_derived_formula("") == []
    assert parse_derived_formula("   ") == []
    assert parse_derived_formula(None) == []


def test_single_metric_ref_with_single_quotes():
    result = parse_derived_formula("metric('sales_amount')")
    assert result == [MetricRef(metric_key="sales_amount")]


def test_single_metric_ref_with_double_quotes():
    result = parse_derived_formula('metric("sales_amount")')
    assert result == [MetricRef(metric_key="sales_amount")]


def test_multiple_different_metric_refs():
    result = parse_derived_formula("metric('a') + metric('b') / metric('c')")
    assert result == [
        MetricRef(metric_key="a"),
        MetricRef(metric_key="b"),
        MetricRef(metric_key="c"),
    ]


def test_duplicate_metric_refs_are_deduplicated():
    result = parse_derived_formula("metric('x') + metric('x') - metric('x')")
    assert result == [MetricRef(metric_key="x")]


def test_nested_parentheses_in_formula():
    result = parse_derived_formula("(metric('a') + metric('b')) * 2")
    assert result == [
        MetricRef(metric_key="a"),
        MetricRef(metric_key="b"),
    ]


def test_mixed_field_and_metric_refs():
    result = parse_derived_formula('SUM("amount") / metric("order_count")')
    assert result == [MetricRef(metric_key="order_count")]


def test_no_metric_ref_returns_empty():
    result = parse_derived_formula('SUM("amount")')
    assert result == []


def test_invalid_metric_syntax_no_quotes():
    with pytest.raises(InvalidFormulaError, match="Invalid metric reference syntax"):
        parse_derived_formula("metric(sales_amount)")


def test_empty_metric_key_raises_error():
    with pytest.raises(InvalidFormulaError, match="Empty metric key"):
        parse_derived_formula("metric('')")


def test_validate_unmatched_parentheses():
    assert validate_formula_syntax("metric('a' + (") is False
    assert validate_formula_syntax("metric('a') + )") is False


def test_validate_matched_parentheses():
    assert validate_formula_syntax("metric('a') + (metric('b') * 2)") is True
    assert validate_formula_syntax("") is True
    assert validate_formula_syntax(None) is True


def test_cycle_detection_simple_two_node():
    all_metrics = {
        "A": "metric('B')",
        "B": "metric('A')",
    }
    cycles = detect_cycle("A", all_metrics)
    assert len(cycles) == 1
    assert cycles[0] == ["A", "B", "A"]


def test_cycle_detection_three_node():
    all_metrics = {
        "A": "metric('B')",
        "B": "metric('C')",
        "C": "metric('A')",
    }
    cycles = detect_cycle("A", all_metrics)
    assert len(cycles) == 1
    assert cycles[0] == ["A", "B", "C", "A"]


def test_no_cycle_returns_empty():
    all_metrics = {
        "A": "metric('B')",
        "B": "metric('D')",
        "D": "SUM(amount)",
    }
    cycles = detect_cycle("A", all_metrics)
    assert cycles == []


def test_self_cycle_detected():
    all_metrics = {
        "A": "metric('A')",
    }
    cycles = detect_cycle("A", all_metrics)
    assert len(cycles) == 1
    assert cycles[0] == ["A", "A"]


def test_cycle_detection_skips_visited_non_cycle_path():
    all_metrics = {
        "A": "metric('B')",
        "B": "metric('C') + metric('D')",
        "C": "SUM(x)",
        "D": "metric('E')",
        "E": "metric('C')",
    }
    cycles = detect_cycle("A", all_metrics)
    assert cycles == []


def test_metric_key_with_special_characters():
    result = parse_derived_formula("metric('sales_2024_q1') + metric('profit-margin')")
    assert result == [
        MetricRef(metric_key="sales_2024_q1"),
        MetricRef(metric_key="profit-margin"),
    ]


def test_parse_derived_formula_preserves_metric_ref_order():
    result = parse_derived_formula("metric('z') + metric('a') + metric('m')")
    assert result == [
        MetricRef(metric_key="z"),
        MetricRef(metric_key="a"),
        MetricRef(metric_key="m"),
    ]