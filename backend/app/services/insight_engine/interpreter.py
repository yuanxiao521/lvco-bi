"""LLM 异常解读器 - 把 detector 的 Anomaly 列表 + 数据点 转成叙述报告"""

import json
import logging
import re
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.ai_prompts import INSIGHT_REPORT_SYSTEM
from app.services.insight_engine.detector import Anomaly, TimePoint
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient


log = logging.getLogger(__name__)

# 解析失败时返回的哨兵 narrative，interpret 据此触发 _fallback_narrative
_PARSE_FAILED_SENTINEL = "(LLM 响应解析失败)"


@dataclass
class InterpretResult:
    """LLM 解读结果"""
    narrative: str                # Markdown 叙述
    summary: str                  # 一句话总结
    highlights: list[dict]        # [{"type","title","description","severity"}]
    llm_model: str | None = None
    llm_tokens_input: int | None = None
    llm_tokens_output: int | None = None
    raw_response: str | None = None  # 调试用


class LLMInterpreter:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    async def interpret(
        self,
        anomalies: list[Anomaly],
        current: list[TimePoint],
        historical: list[TimePoint],
        query_config: dict,
    ) -> InterpretResult:
        """解读异常并生成叙述报告

        Args:
            anomalies: detector.detect_anomalies() 的结果
            current: 当前周期的数据点（最近一段时间，用于展示）
            historical: 历史数据点（用于趋势描述）
            query_config: 规则的查询配置（含 table / time_field / measures / dimensions）
        """
        # 1. 检查 LLM 是否配置
        try:
            self.llm._check_configured()
        except AINotConfiguredError:
            log.info("LLM not configured, falling back to rule-based narrative")
            return self._fallback_narrative(anomalies, query_config)

        # 2. 构造 prompt + messages
        user_prompt = self._build_user_prompt(anomalies, current, historical, query_config)
        messages = [
            {"role": "system", "content": INSIGHT_REPORT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        # 3. 调用 LLM，捕获上游异常
        try:
            content = await self.llm.complete(
                messages, temperature=0.4, max_tokens=1500
            )
        except AIUpstreamError as e:
            log.warning("LLM upstream error, falling back: %s", e)
            return self._fallback_narrative(anomalies, query_config)
        except Exception as e:  # 防御性兜底，绝不让 LLM 失败拖垮洞察流程
            log.warning("LLM call failed, falling back: %s", e)
            return self._fallback_narrative(anomalies, query_config)

        # 4. 解析响应；解析失败也走 fallback
        result = self._parse_response(content)
        if result.narrative == _PARSE_FAILED_SENTINEL:
            log.warning("LLM response unparseable, falling back to rule-based narrative")
            return self._fallback_narrative(anomalies, query_config)
        result.raw_response = content
        return result

    def _build_user_prompt(
        self,
        anomalies: list[Anomaly],
        current: list[TimePoint],
        historical: list[TimePoint],
        query_config: dict,
    ) -> str:
        """构造 user prompt"""
        sections: list[str] = []

        # ## 查询配置
        table = query_config.get("table", "")
        time_field = query_config.get("time_field", "")
        measures = query_config.get("measures", []) or []
        dimensions = query_config.get("dimensions", []) or []
        time_range_days = query_config.get("time_range_days", "")

        cfg_lines = [
            f"- table: `{table}`",
            f"- time_field: `{time_field}`",
            f"- time_range_days: {time_range_days}",
        ]
        if measures:
            measure_strs = [
                f"{m.get('field', '?')}({m.get('agg', '?')})" for m in measures
            ]
            cfg_lines.append(f"- measures: {', '.join(measure_strs)}")
        else:
            cfg_lines.append("- measures: (无)")
        if dimensions:
            cfg_lines.append(f"- dimensions: {', '.join(dimensions)}")
        else:
            cfg_lines.append("- dimensions: (无)")
        sections.append("## 查询配置\n" + "\n".join(cfg_lines))

        # ## 异常列表
        if anomalies:
            anomaly_lines = [f"共 {len(anomalies)} 条异常："]
            for i, a in enumerate(anomalies, 1):
                anomaly_lines.append(
                    f"{i}. type={a.type.value} | field={a.field} | severity={a.severity.value} | "
                    f"current_value={a.current_value} | expected_value={a.expected_value} | "
                    f"deviation={a.deviation:+.4f} | direction={a.direction}\n"
                    f"   description: {a.description}"
                )
            sections.append("## 异常列表\n" + "\n".join(anomaly_lines))
        else:
            sections.append("## 异常列表\n无异常")

        # ## 当前周期数据
        measure_fields = [m.get("field", "") for m in measures if m.get("field")]
        if current:
            current_lines = [f"最近 {len(current)} 天数据（最多 30 行）："]
            for p in current[:30]:
                ts = p.timestamp.strftime("%Y-%m-%d") if isinstance(
                    p.timestamp, datetime
                ) else str(p.timestamp)
                if measure_fields:
                    vals = ", ".join(
                        f"{f}={p.values.get(f, '')}" for f in measure_fields
                    )
                else:
                    vals = ", ".join(f"{k}={v}" for k, v in p.values.items())
                current_lines.append(f"- {ts}: {vals}")
            sections.append("## 当前周期数据 (最近 N 天)\n" + "\n".join(current_lines))
        else:
            sections.append("## 当前周期数据 (最近 N 天)\n(无当前周期数据)")

        # ## 历史趋势摘要
        if historical and measure_fields:
            hist_lines = []
            for f in measure_fields:
                vals = [p.values.get(f) for p in historical if p.values.get(f) is not None]
                if not vals:
                    hist_lines.append(f"- {f}: 无数据")
                    continue
                mn = min(vals)
                mx = max(vals)
                avg = statistics.mean(vals)
                last = vals[-1]
                recent_7 = vals[-7:] if len(vals) >= 7 else vals
                recent_avg = statistics.mean(recent_7)
                hist_lines.append(
                    f"- {f}: min={mn:.4f}, max={mx:.4f}, avg={avg:.4f}, "
                    f"last={last:.4f}, 最近7天均值={recent_avg:.4f}"
                )
            sections.append("## 历史趋势摘要\n" + "\n".join(hist_lines))
        else:
            sections.append("## 历史趋势摘要\n(无历史数据)")

        return "\n\n".join(sections)

    def _parse_response(self, content: str) -> InterpretResult:
        """解析 LLM 的 JSON 响应。解析失败时返回哨兵结果（不抛异常），
        上层 interpret 检测哨兵后会调用 _fallback_narrative。"""
        text = content or ""
        data: Any = None

        # 第一次尝试：直接 json.loads
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None

        # 第二次尝试：提取 ```json ... ``` 代码块
        if data is None:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                except json.JSONDecodeError:
                    data = None

        # 第三次尝试：提取最外层 {...}
        if data is None:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None

        # 完全无法解析 → 返回哨兵结果（不抛异常）
        if not isinstance(data, dict):
            return InterpretResult(
                narrative=_PARSE_FAILED_SENTINEL,
                summary=_PARSE_FAILED_SENTINEL,
                highlights=[],
                raw_response=content,
            )

        narrative = data.get("narrative")
        if not isinstance(narrative, str) or not narrative.strip():
            narrative = "(LLM 未返回有效叙述)"
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            summary = "(LLM 未返回有效摘要)"
        highlights_raw = data.get("highlights")
        if not isinstance(highlights_raw, list):
            highlights_raw = []

        highlights: list[dict] = []
        for h in highlights_raw:
            if not isinstance(h, dict):
                continue
            title = h.get("title")
            description = h.get("description")
            if not isinstance(title, str) and not isinstance(description, str):
                continue
            highlights.append({
                "type": h.get("type") if isinstance(h.get("type"), str) else "summary",
                "title": title if isinstance(title, str) else "",
                "description": description if isinstance(description, str) else "",
                "severity": h.get("severity") if isinstance(h.get("severity"), str) else "info",
            })

        return InterpretResult(
            narrative=narrative,
            summary=summary,
            highlights=highlights,
            llm_model=None,
            llm_tokens_input=None,
            llm_tokens_output=None,
            raw_response=content,
        )

    def _fallback_narrative(self, anomalies: list[Anomaly], query_config: dict) -> InterpretResult:
        """LLM 不可用或解析失败时的兜底，基于规则拼叙述"""
        table = query_config.get("table", "")
        measures = query_config.get("measures", []) or []
        measure_strs = [
            f"{m.get('field', '?')}({m.get('agg', '?')})" for m in measures
        ]

        direction_zh = {"up": "上升", "down": "下降"}

        if anomalies:
            # 找最严重的一条
            severity_order = {"critical": 3, "warning": 2, "info": 1}
            worst = max(
                anomalies,
                key=lambda a: severity_order.get(a.severity.value, 0),
            )
            worst_dir = direction_zh.get(worst.direction, worst.direction)
            summary = (
                f"检测到 {len(anomalies)} 条异常，最严重: {worst.field} {worst_dir}"
            )
            if len(summary) > 80:
                summary = summary[:77] + "..."

            anomaly_lines = []
            for i, a in enumerate(anomalies, 1):
                a_dir = direction_zh.get(a.direction, a.direction)
                anomaly_lines.append(
                    f"{i}. **{a.field}** {a_dir} "
                    f"(current={a.current_value}, expected={a.expected_value}, "
                    f"deviation={a.deviation:+.1%}, severity={a.severity.value})\n"
                    f"   {a.description}"
                )
            narrative = (
                f"## 异常摘要\n"
                f"在 `{table}` 上检测到 **{len(anomalies)} 条异常**：\n\n"
                + "\n".join(anomaly_lines)
                + f"\n\n## 总体趋势\n"
                f"> 由于 LLM 不可用，趋势部分由规则生成占位说明。"
                f"监控指标：{', '.join(measure_strs) if measure_strs else '（未配置）'}。"
                f"建议结合上方异常列表与历史数据进一步分析。"
            )

            highlights = [
                {
                    "type": "anomaly",
                    "title": f"{a.field} {direction_zh.get(a.direction, a.direction)}",
                    "description": a.description,
                    "severity": a.severity.value,
                }
                for a in anomalies
            ]
        else:
            summary = "运行平稳，未检测到明显异常"
            narrative = (
                f"## 异常摘要\n"
                f"`{table}` 运行平稳，未检测到明显异常。\n\n"
                f"## 总体趋势\n"
                f"> 由于 LLM 不可用，趋势部分由规则生成占位说明。"
                f"监控指标：{', '.join(measure_strs) if measure_strs else '（未配置）'}。"
            )
            highlights = []

        return InterpretResult(
            narrative=narrative,
            summary=summary,
            highlights=highlights,
            llm_model=None,
            llm_tokens_input=None,
            llm_tokens_output=None,
            raw_response=None,
        )
