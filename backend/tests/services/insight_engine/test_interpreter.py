"""LLMInterpreter 测试"""

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from app.services.insight_engine.detector import (
    Anomaly,
    AnomalyType,
    Severity,
    TimePoint,
)
from app.services.insight_engine.interpreter import (
    LLMInterpreter,
    InterpretResult,
)
from app.services.llm_client import AINotConfiguredError, AIUpstreamError


# ---------- 公共 fixture ----------

def _query_config() -> dict:
    return {
        "table": "orders",
        "time_field": "created_at",
        "measures": [{"field": "amount", "agg": "SUM"}],
        "dimensions": [],
        "time_range_days": 30,
    }


def _anomaly() -> Anomaly:
    return Anomaly(
        type=AnomalyType.z_score,
        field="amount",
        severity=Severity.warning,
        current_value=250.0,
        expected_value=100.0,
        deviation=1.5,
        direction="up",
        description="z-score=3.2，amount 偏离均值 +150%",
    )


def _time_points(n: int = 10, field: str = "amount", base: float = 100.0) -> list[TimePoint]:
    """生成 n 个时间点，每天一个，值都是 base（便于测试）"""
    start = datetime(2026, 7, 1)
    return [
        TimePoint(timestamp=start + timedelta(days=i), values={field: base})
        for i in range(n)
    ]


# ---------- _build_user_prompt 测试 ----------

def test_build_user_prompt_includes_config():
    """prompt 应包含 table / time_field / measure 字段"""
    interp = LLMInterpreter(llm=MagicMock())
    prompt = interp._build_user_prompt(
        anomalies=[],
        current=_time_points(),
        historical=_time_points(20),
        query_config=_query_config(),
    )
    assert "orders" in prompt
    assert "created_at" in prompt
    assert "amount" in prompt
    assert "SUM" in prompt
    assert "30" in prompt  # time_range_days


def test_build_user_prompt_includes_anomalies():
    """prompt 应包含异常描述"""
    interp = LLMInterpreter(llm=MagicMock())
    anomaly = _anomaly()
    prompt = interp._build_user_prompt(
        anomalies=[anomaly],
        current=_time_points(),
        historical=_time_points(20),
        query_config=_query_config(),
    )
    assert "amount" in prompt
    assert "250.0" in prompt
    assert "100.0" in prompt
    assert "z-score=3.2" in prompt  # description 片段
    assert "warning" in prompt  # severity


def test_build_user_prompt_empty_anomalies():
    """无异常时 prompt 应含 '无异常'"""
    interp = LLMInterpreter(llm=MagicMock())
    prompt = interp._build_user_prompt(
        anomalies=[],
        current=_time_points(),
        historical=_time_points(20),
        query_config=_query_config(),
    )
    assert "无异常" in prompt


# ---------- _parse_response 测试 ----------

def test_parse_response_valid_json():
    """给定合法 JSON 字符串，正确解析"""
    interp = LLMInterpreter(llm=MagicMock())
    payload = {
        "narrative": "## 概览\n金额异常上升。",
        "summary": "amount 异常上涨 150%。",
        "highlights": [
            {"type": "anomaly", "title": "amount 上升",
             "description": "涨幅 150%", "severity": "warning"},
            {"type": "trend", "title": "整体上升",
             "description": "近 7 天上行", "severity": "info"},
        ],
    }
    result = interp._parse_response(json.dumps(payload, ensure_ascii=False))
    assert isinstance(result, InterpretResult)
    assert "金额异常" in result.narrative
    assert "amount" in result.summary
    assert len(result.highlights) == 2
    assert result.highlights[0]["type"] == "anomaly"
    assert result.highlights[0]["severity"] == "warning"
    assert result.highlights[1]["type"] == "trend"


def test_parse_response_with_code_block():
    """给定被 ```json ... ``` 包裹的字符串，能提取并解析"""
    interp = LLMInterpreter(llm=MagicMock())
    payload = {
        "narrative": "## 概览\n运行平稳。",
        "summary": "平稳运行。",
        "highlights": [
            {"type": "trend", "title": "趋势", "description": "稳定", "severity": "info"}
        ],
    }
    body = json.dumps(payload, ensure_ascii=False)
    content = f"以下是分析结果：\n```json\n{body}\n```\n"
    result = interp._parse_response(content)
    assert "运行平稳" in result.narrative
    assert result.summary == "平稳运行。"
    assert len(result.highlights) == 1


def test_parse_response_invalid_falls_back():
    """给定乱码，返回 fallback 结果（不抛异常）"""
    interp = LLMInterpreter(llm=MagicMock())
    result = interp._parse_response("这不是 JSON，无法解析")
    assert isinstance(result, InterpretResult)
    # 解析失败时返回哨兵结果，不抛异常
    assert result.highlights == []
    assert result.narrative  # 非空占位
    assert result.summary    # 非空占位


# ---------- _fallback_narrative 测试 ----------

def test_fallback_narrative_with_anomalies():
    """给定异常列表，生成包含异常描述的叙述"""
    interp = LLMInterpreter(llm=MagicMock())
    anomaly = _anomaly()
    result = interp._fallback_narrative([anomaly], _query_config())
    assert isinstance(result, InterpretResult)
    assert "1 条异常" in result.summary or "检测到 1 条异常" in result.summary
    assert "amount" in result.summary
    assert "上升" in result.summary  # direction 中文化
    assert "## 异常摘要" in result.narrative
    assert "## 总体趋势" in result.narrative
    assert "amount" in result.narrative
    assert "250.0" in result.narrative  # current_value
    assert len(result.highlights) == 1
    assert result.highlights[0]["type"] == "anomaly"
    assert result.highlights[0]["severity"] == "warning"
    assert "amount" in result.highlights[0]["title"]
    assert "上升" in result.highlights[0]["title"]  # title 也中文化
    assert result.raw_response is None


def test_fallback_narrative_no_anomalies():
    """无异常时 summary 含 '平稳'"""
    interp = LLMInterpreter(llm=MagicMock())
    result = interp._fallback_narrative([], _query_config())
    assert "平稳" in result.summary
    assert "## 异常摘要" in result.narrative
    assert "## 总体趋势" in result.narrative
    assert result.highlights == []


# ---------- interpret 异步测试（用 asyncio.run 包裹，无需 pytest-asyncio） ----------

def test_interpret_success_with_mock_llm():
    """mock LLMClient.complete 返回合法 JSON，验证 interpret 返回正确结构"""
    mock_llm = MagicMock()
    payload = {
        "narrative": "## 概览\namount 出现异常上升。\n\n## 建议\n> 关注后续走势。",
        "summary": "amount 异常上涨 150%，需关注。",
        "highlights": [
            {"type": "anomaly", "title": "amount 异常上升",
             "description": "z-score=3.2", "severity": "warning"},
            {"type": "trend", "title": "整体趋势",
             "description": "近 7 天上行", "severity": "info"},
        ],
    }
    mock_llm.complete = AsyncMock(return_value=json.dumps(payload, ensure_ascii=False))
    mock_llm._check_configured = MagicMock()  # 不抛即代表已配置
    interp = LLMInterpreter(llm=mock_llm)

    result = asyncio.run(interp.interpret(
        anomalies=[_anomaly()],
        current=_time_points(),
        historical=_time_points(20),
        query_config=_query_config(),
    ))

    assert isinstance(result, InterpretResult)
    assert "amount" in result.narrative
    assert "150%" in result.summary
    assert len(result.highlights) == 2
    assert result.highlights[0]["type"] == "anomaly"
    # 验证 complete 被正确调用
    mock_llm.complete.assert_awaited_once()
    call_args = mock_llm.complete.await_args
    messages = call_args.args[0]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # temperature / max_tokens 透传
    assert call_args.kwargs.get("temperature") == 0.4
    assert call_args.kwargs.get("max_tokens") == 1500
    # raw_response 保留原始内容
    assert result.raw_response is not None
    assert "amount" in result.raw_response


def test_interpret_llm_not_configured_falls_back():
    """mock LLMClient._check_configured 抛 AINotConfiguredError，验证 fallback"""
    mock_llm = MagicMock()
    mock_llm._check_configured = MagicMock(
        side_effect=AINotConfiguredError("no key")
    )
    mock_llm.complete = AsyncMock(return_value='{}')
    interp = LLMInterpreter(llm=mock_llm)

    result = asyncio.run(interp.interpret(
        anomalies=[_anomaly()],
        current=_time_points(),
        historical=_time_points(20),
        query_config=_query_config(),
    ))

    # 应该走 fallback
    assert isinstance(result, InterpretResult)
    assert "检测到 1 条异常" in result.summary
    assert "## 异常摘要" in result.narrative
    assert len(result.highlights) == 1
    assert result.highlights[0]["type"] == "anomaly"
    # complete 不应该被调用
    mock_llm.complete.assert_not_awaited()


def test_interpret_llm_upstream_error_falls_back():
    """mock LLMClient.complete 抛 AIUpstreamError，验证 fallback"""
    mock_llm = MagicMock()
    mock_llm._check_configured = MagicMock()  # 已配置
    mock_llm.complete = AsyncMock(side_effect=AIUpstreamError("500 internal"))
    interp = LLMInterpreter(llm=mock_llm)

    result = asyncio.run(interp.interpret(
        anomalies=[_anomaly()],
        current=_time_points(),
        historical=_time_points(20),
        query_config=_query_config(),
    ))

    assert isinstance(result, InterpretResult)
    assert "检测到 1 条异常" in result.summary
    assert "## 异常摘要" in result.narrative
    assert len(result.highlights) == 1
    assert result.highlights[0]["type"] == "anomaly"
    mock_llm.complete.assert_awaited_once()
