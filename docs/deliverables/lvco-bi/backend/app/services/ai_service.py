import json
import logging
import re
from collections.abc import AsyncIterator

from app.config import Settings, settings
from app.services.ai_prompts import (
    CHAT_SYSTEM,
    CLEAN_SYSTEM,
    INSIGHTS_SYSTEM,
    POLISH_SYSTEM,
    RECOMMEND_SYSTEM,
)
from app.services.llm_client import AINotConfiguredError, AIUpstreamError, LLMClient

log = logging.getLogger("lvco.ai_service")


_SEV_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_INSIGHT_SEV_ORDER: dict[str, int] = {"warning": 0, "success": 1, "info": 2}


def _extract_json_array(text: str) -> list | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = text[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_time_like(field: dict | None) -> bool:
    if not isinstance(field, dict):
        return False
    name = str(field.get("name") or field.get("field") or "").lower()
    ftype = str(field.get("type") or field.get("data_type") or field.get("dtype") or "").lower()
    if any(token in ftype for token in ("date", "time", "timestamp", "datetime")):
        return True
    if any(token in name for token in ("date", "time", "timestamp", "month", "year", "day")):
        return True
    return False


def _meta_lookup(field_meta: list[dict] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in field_meta or []:
        if isinstance(f, dict):
            name = f.get("name") or f.get("field")
            if isinstance(name, str):
                out[name] = f
    return out


def _normalize_measures(current_config: dict) -> list[dict]:
    raw = current_config.get("measures") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for m in raw:
        if isinstance(m, str):
            out.append({"field": m, "agg": "SUM"})
        elif isinstance(m, dict):
            field = m.get("field") or m.get("name") or m.get("alias")
            agg = m.get("agg") or "SUM"
            if isinstance(field, str):
                out.append({"field": field, "agg": agg})
    return out


def _normalize_dimensions(current_config: dict) -> list[str]:
    raw = current_config.get("dimensions") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for d in raw:
        if isinstance(d, str):
            out.append(d)
        elif isinstance(d, dict):
            name = d.get("field") or d.get("name") or d.get("alias")
            if isinstance(name, str):
                out.append(name)
    return out


def _confidence_for(rule: str) -> float:
    return {
        "kpi": 0.9,
        "time_line": 0.85,
        "grouped": 0.82,
        "single_dim": 0.78,
        "default": 0.7,
    }.get(rule, 0.7)


def _build_config(current_config: dict, chart_type: str) -> dict:
    cfg = dict(current_config or {})
    cfg["chartType"] = chart_type
    cfg["chart_type"] = chart_type
    return cfg


class AIService:
    def __init__(self, llm: LLMClient | None = None, settings_override: Settings | None = None) -> None:
        cfg = settings_override or settings
        self.llm = llm or LLMClient(cfg)

    async def chat_stream(
        self,
        history: list[dict[str, str]],
        user_msg: str,
    ) -> AsyncIterator[str]:
        messages: list[dict[str, str]] = [{"role": "system", "content": CHAT_SYSTEM}]
        for h in history or []:
            if not isinstance(h, dict):
                continue
            role = h.get("role")
            content = h.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_msg})

        async for token in self.llm.stream_chat(messages, temperature=0.5):
            yield token

    async def recommend_charts(
        self,
        field_meta: list[dict],
        current_config: dict,
    ) -> list[dict]:
        dimensions = _normalize_dimensions(current_config)
        measures = _normalize_measures(current_config)

        if not measures:
            raise ValueError("请至少添加一个度量")

        # 先尝试调用 LLM 给真正的 AI 推荐，失败时回退到规则匹配
        llm_suggestions = await self._recommend_charts_llm(field_meta, current_config)
        if llm_suggestions:
            return llm_suggestions

        # 回退：基于规则的快速推荐
        return self._recommend_charts_by_rules(field_meta, current_config)

    async def _recommend_charts_llm(
        self,
        field_meta: list[dict],
        current_config: dict,
    ) -> list[dict]:
        """调用 LLM 生成图表推荐。LLM 不可用或返回无效时返回空列表，由调用方回退到规则。"""
        try:
            self.llm._check_configured()
        except AINotConfiguredError:
            log.info("LLM 未配置，使用规则推荐")
            return []

        try:
            messages = [
                {"role": "system", "content": RECOMMEND_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "## 字段元信息\n"
                        + json.dumps(field_meta or [], ensure_ascii=False, default=str)
                        + "\n\n## 当前配置\n"
                        + json.dumps(current_config or {}, ensure_ascii=False, default=str)
                        + "\n\n请基于以上字段和当前配置，推荐 1-3 个最合适的图表类型，"
                        "并返回严格 JSON 数组。"
                    ),
                },
            ]
            content = await self.llm.complete(messages, temperature=0.4, max_tokens=800)
            suggestions = _extract_json_array(content) or []
            normalized: list[dict] = []
            for s in suggestions:
                if not isinstance(s, dict):
                    continue
                ct = s.get("chart_type") or s.get("chartType")
                if not isinstance(ct, str):
                    continue
                rationale = s.get("rationale") or "AI 推荐"
                confidence = s.get("confidence")
                try:
                    conf = float(confidence) if confidence is not None else 0.8
                except (TypeError, ValueError):
                    conf = 0.8
                # config 既可来自 LLM，也可从 current_config 推导
                cfg = s.get("config")
                if not isinstance(cfg, dict):
                    cfg = _build_config(current_config, ct)
                else:
                    cfg = dict(cfg)
                    cfg["chartType"] = ct
                    cfg["chart_type"] = ct
                normalized.append(
                    {
                        "chart_type": ct,
                        "rationale": rationale,
                        "config": cfg,
                        "confidence": max(0.0, min(1.0, conf)),
                    }
                )
            if normalized:
                log.info("LLM 推荐图表成功，返回 %d 条", len(normalized))
                return normalized
            log.warning("LLM 返回的 JSON 无法解析为推荐列表，使用规则推荐")
            return []
        except (AINotConfiguredError, AIUpstreamError) as e:
            log.warning("LLM 推荐失败，使用规则推荐: %s", e)
            return []
        except Exception as e:  # noqa: BLE001
            log.exception("LLM 推荐异常，使用规则推荐: %s", e)
            return []

    def _recommend_charts_by_rules(
        self,
        field_meta: list[dict],
        current_config: dict,
    ) -> list[dict]:
        dimensions = _normalize_dimensions(current_config)
        measures = _normalize_measures(current_config)
        meta = _meta_lookup(field_meta)
        time_dim = next(
            (d for d in dimensions if _is_time_like(meta.get(d))),
            None,
        )

        if not dimensions and measures:
            rationale = "仅有一个度量值，无维度拆解，使用 KPI 卡片直陈关键指标"
            return [
                {
                    "chart_type": "kpi_card",
                    "rationale": rationale,
                    "config": _build_config(current_config, "kpi_card"),
                    "confidence": _confidence_for("kpi"),
                }
            ]

        if time_dim and len(dimensions) >= 1 and len(measures) == 1:
            suggestions: list[dict] = []
            for chart_type, conf_rule in (
                ("line", "time_line"),
                ("bar", "single_dim"),
                ("area", "default"),
            ):
                rationale = {
                    "line": "含时间维度，折线图最能展现趋势变化",
                    "bar": "也可使用柱状图按时间粒度对比度量值",
                    "area": "面积图强调累计或分层占比",
                }[chart_type]
                suggestions.append(
                    {
                        "chart_type": chart_type,
                        "rationale": rationale,
                        "config": _build_config(current_config, chart_type),
                        "confidence": _confidence_for(conf_rule),
                    }
                )
            return suggestions

        if len(dimensions) == 1 and len(measures) >= 2:
            suggestions = []
            for chart_type, conf_rule in (
                ("grouped_bar", "grouped"),
                ("stacked_bar", "single_dim"),
                ("line", "default"),
            ):
                rationale = {
                    "grouped_bar": "一个维度 + 多个度量，分组柱状便于横向对比",
                    "stacked_bar": "若关注组成占比，使用堆叠柱状图",
                    "line": "折线图展示多指标随维度的变化趋势",
                }[chart_type]
                suggestions.append(
                    {
                        "chart_type": chart_type,
                        "rationale": rationale,
                        "config": _build_config(current_config, chart_type),
                        "confidence": _confidence_for(conf_rule),
                    }
                )
            return suggestions

        suggestions = []
        for chart_type, conf_rule in (
            ("bar", "single_dim"),
            ("line", "default"),
            ("pie", "default"),
        ):
            rationale = {
                "bar": "柱状图是最通用的对比图表",
                "line": "折线图展示数据走势",
                "pie": "饼图展示占比构成",
            }[chart_type]
            suggestions.append(
                {
                    "chart_type": chart_type,
                    "rationale": rationale,
                    "config": _build_config(current_config, chart_type),
                    "confidence": _confidence_for(conf_rule),
                }
            )
        return suggestions

    async def clean_suggest(
        self,
        field_meta: list[dict],
        duckdb_stats: list[dict],
    ) -> list[dict]:
        prompt_user = (
            "Field meta: "
            + json.dumps(field_meta or [], ensure_ascii=False, default=str)
            + "\nDuckDB stats: "
            + json.dumps(duckdb_stats or [], ensure_ascii=False, default=str)
            + "\n请给出清洗建议，按 severity 高→低排序。"
        )

        try:
            content = await self.llm.complete(
                [
                    {"role": "system", "content": CLEAN_SYSTEM},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=0.2,
            )
        except (AINotConfiguredError, AIUpstreamError) as e:
            log.warning("LLM clean_suggest fallback: %s", e)
            return _fallback_clean(duckdb_stats)

        raw_list = _extract_json_array(content)
        if raw_list is None:
            parsed_obj = _extract_json_object(content)
            if isinstance(parsed_obj, dict):
                inner = parsed_obj.get("issues")
                if isinstance(inner, list):
                    raw_list = inner
        if raw_list is None:
            return _fallback_clean(duckdb_stats)

        issues: list[dict] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity") or "low").lower()
            if severity not in _SEV_ORDER:
                severity = "low"
            issues.append(
                {
                    "field": item.get("field"),
                    "issue_type": item.get("issue_type"),
                    "suggestion": item.get("suggestion"),
                    "severity": severity,
                }
            )
        issues.sort(key=lambda x: _SEV_ORDER.get(x.get("severity", "low"), 3))
        return issues

    async def generate_insights(
        self,
        agg_result: list[dict],
        query_config: dict,
    ) -> list[dict]:
        top_rows = list(agg_result or [])[:200]

        log.info("generate_insights: received %d rows, first row: %s",
                 len(agg_result or []),
                 json.dumps(agg_result[0] if agg_result else {}, ensure_ascii=False, default=str))

        # Build explicit top-N summary to prevent LLM from missing headline data
        summary_lines: list[str] = []
        if top_rows:
            # Extract dimension and measure keys from the first row
            first_row = top_rows[0]
            keys = list(first_row.keys())
            dim_key = keys[0] if keys else ""
            measure_keys = keys[1:] if len(keys) > 1 else []

            if dim_key and measure_keys:
                mkey = measure_keys[0]
                # Top 5 summary
                summary_lines.append("数据 TOP 5（务必在洞察中提及）:")
                for idx, row in enumerate(top_rows[:5]):
                    dim_val = row.get(dim_key, "?")
                    meas_val = row.get(mkey, 0)
                    # Format large numbers for readability
                    if isinstance(meas_val, (int, float)):
                        if abs(meas_val) >= 1_0000_0000:
                            meas_str = f"{meas_val / 1_0000_0000:.2f}亿"
                        elif abs(meas_val) >= 1_0000:
                            meas_str = f"{meas_val / 1_0000:.2f}万"
                        else:
                            meas_str = f"{meas_val:,.2f}"
                    else:
                        meas_str = str(meas_val)
                    summary_lines.append(f"  {idx + 1}. {dim_val} = {meas_str}")
                summary_lines.append("")

        prompt_user = (
            "Query config: "
            + json.dumps(query_config or {}, ensure_ascii=False, default=str)
            + "\n" + "\n".join(summary_lines)
            + "\n完整聚合结果（前200行）: "
            + json.dumps(top_rows, ensure_ascii=False, default=str)
            + "\n请生成 2-5 条洞察，至少包含 1 条 trend。务必提及排名第一的数据（TOP 1）。"
        )

        try:
            content = await self.llm.complete(
                [
                    {"role": "system", "content": INSIGHTS_SYSTEM},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=0.4,
                max_tokens=2000,
            )
        except (AINotConfiguredError, AIUpstreamError) as e:
            log.warning("LLM generate_insights fallback: %s", e)
            return []

        raw_list = _extract_json_array(content)
        if raw_list is None:
            parsed_obj = _extract_json_object(content)
            if isinstance(parsed_obj, dict):
                inner = parsed_obj.get("insights")
                if isinstance(inner, list):
                    raw_list = inner
        if not isinstance(raw_list, list):
            return []

        insights: list[dict] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity") or "info").lower()
            if severity not in _INSIGHT_SEV_ORDER:
                severity = "info"
            insights.append(
                {
                    "type": item.get("type"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "severity": severity,
                    "related_fields": item.get("related_fields") or [],
                }
            )
        return insights

    async def polish_text(self, text: str, style: str) -> dict:
        prompt_user = (
            f"Style: {style}\n"
            f"Original text: {text}\n"
            "请润色以上文本，按指定风格输出 polished 字段。"
        )

        try:
            content = await self.llm.complete(
                [
                    {"role": "system", "content": POLISH_SYSTEM},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=0.5,
            )
        except (AINotConfiguredError, AIUpstreamError) as e:
            log.warning("LLM polish_text fallback: %s", e)
            polished = text
            parsed_obj: dict | None = None
        else:
            parsed_obj = _extract_json_object(content)
            polished_raw = parsed_obj.get("polished") if isinstance(parsed_obj, dict) else None
            polished = polished_raw if isinstance(polished_raw, str) and polished_raw else content.strip()

        return {
            "original": text,
            "polished": polished,
            "style": style,
        }

    async def agent_stream(
        self,
        user_id: str,
        user_msg: str,
        history: list[dict[str, str]],
        db_session,
        initial_phase: str = "selecting",
    ) -> AsyncIterator[dict]:
        """Agent 循环：Phase 状态机 + ReAct，按阶段控制工具可用性。

        Phase 流转：selecting → analyzing → generating → reporting

        Yield 事件:
            {"type": "text", "content": "..."}
            {"type": "tool_call", "name": "...", "args": {...}}
            {"type": "tool_result", "name": "...", "result": "..."}
            {"type": "chart", "chart_type": "...", "option": {...}}
            {"type": "done"}
            {"type": "error", "message": "..."}
        """
        from app.services.agent_tools import ToolRegistry, ConversationPhase, get_tools_for_phase
        from app.services.ai_prompts import AGENT_SYSTEM
        from app.services.sql_guard import sql_guard, GuardResult

        # L1 + L2: 安全检查
        guard_result: GuardResult = sql_guard.full_check(user_msg)
        if not guard_result.allowed:
            yield {"type": "error", "message": guard_result.reason}
            return

        all_tools = ToolRegistry.schemas()
        phase = ConversationPhase(initial_phase)
        MAX_ITERATIONS = 6
        MAX_CONSECUTIVE_FAILURES = 5  # 连续失败熔断

        messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM}]
        for h in (history or [])[-20:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                messages.append({"role": h["role"], "content": str(h.get("content", ""))})
        messages.append({"role": "user", "content": guard_result.sanitized_input or user_msg})

        executed_tool_names: list[str] = []  # 记录本轮对话中执行过的工具
        consecutive_query_failures = 0  # 连续查询失败计数器

        for iteration in range(MAX_ITERATIONS):
            tool_calls_in_turn: list[dict] = []
            has_text_output = False

            # 按当前阶段过滤可用工具
            phase_tools = get_tools_for_phase(phase, all_tools)

            async for event in self.llm.stream_chat_with_tools(
                messages, phase_tools, temperature=0.5, max_tokens=3000,
            ):
                if event["type"] == "text":
                    has_text_output = True
                    yield event
                elif event["type"] == "tool_call":
                    tool_calls_in_turn.append(event)
                elif event["type"] == "done":
                    pass

            # 如果本轮 text 输出且无 tool_call，说明 LLM 已经给出最终回复
            if has_text_output and not tool_calls_in_turn:
                yield {"type": "done"}
                return

            # 如果本轮无任何输出且无 tool_call，结束
            if not has_text_output and not tool_calls_in_turn:
                yield {"type": "done"}
                return

            # 执行所有收集到的工具调用
            if not tool_calls_in_turn:
                yield {"type": "done"}
                return

            # 构建 assistant message with tool_calls
            assistant_tool_calls = []
            for tc in tool_calls_in_turn:
                assistant_tool_calls.append({
                    "id": tc.get("id", f"call_{len(assistant_tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc.get("arguments", "{}"),
                    },
                })

            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": assistant_tool_calls,
            })

            # 逐一执行工具并添加 tool result messages
            all_results: list[dict] = []
            has_error = False
            for tc in tool_calls_in_turn:
                tname = tc.get("name", "")
                targs_str = tc.get("arguments", "{}")
                try:
                    targs = json.loads(targs_str) if targs_str else {}
                except json.JSONDecodeError:
                    targs = {}

                executed_tool_names.append(tname)
                yield {"type": "tool_call", "name": tname, "args": targs}

                tool = ToolRegistry.get(tname)
                if tool:
                    try:
                        result = await tool.execute(user_id=user_id, db_session=db_session, **targs)
                        all_results.append({"name": tname, "result": result})
                        yield {"type": "tool_result", "name": tname, "result": result}

                        # 检查查询是否成功
                        result_obj = json.loads(result)
                        has_error = "error" in result_obj
                        if has_error and tname == "query_datasource":
                            consecutive_query_failures += 1
                        elif not has_error and tname == "query_datasource":
                            consecutive_query_failures = 0

                        # chart 事件
                        if tname == "render_chart":
                            try:
                                cr = json.loads(result)
                                if cr.get("option"):
                                    yield {"type": "chart", "chart_type": cr.get("chart_type", "bar"), "option": cr["option"]}
                            except Exception as e:
                                log.warning("render_chart parse failed: %s", e)
                    except Exception as e:
                        err = json.dumps({"error": str(e)})
                        all_results.append({"name": tname, "result": err})
                        yield {"type": "tool_result", "name": tname, "result": err}
                        if tname == "query_datasource":
                            consecutive_query_failures += 1
                        has_error = True
                else:
                    err = json.dumps({"error": f"未知工具: {tname}"})
                    all_results.append({"name": tname, "result": err})
                    yield {"type": "tool_result", "name": tname, "result": err}
                    has_error = True

                # 将工具结果添加到 messages
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{len(messages)}"),
                    "content": all_results[-1]["result"],
                })

            # 熔断：连续查询失败超过阈值，终止循环
            if consecutive_query_failures >= MAX_CONSECUTIVE_FAILURES:
                yield {"type": "text", "content": (
                    f"\n\n> 抱歉，连续 {consecutive_query_failures} 次查询都失败了。"
                    "可能是数据源字段名或表结构与我预期的不一致。"
                    "请检查数据源是否正确连接，或尝试用更简单的查询方式。"
                )}
                yield {"type": "done"}
                return

            # === Phase 状态流转：根据已执行的工具推进阶段 ===
            if phase == ConversationPhase.ANALYZING and not has_error:
                if any(n == "query_datasource" for n in executed_tool_names):
                    phase = ConversationPhase.GENERATING
            elif phase == ConversationPhase.GENERATING:
                if any(n == "render_chart" for n in executed_tool_names):
                    phase = ConversationPhase.REPORTING
            # SELECTING 不自动流转：list_datasources 后 AI 列出数据源即结束
            # REPORTING 不流转：无工具，AI 输出文本后自然结束

            # 将工具结果摘要注入，引导 LLM 输出分析
            results_text = json.dumps(all_results, ensure_ascii=False)

            # 根据执行的工具类型和是否失败定制 follow-up
            if all(n == "list_datasources" for n in executed_tool_names if n == "list_datasources"):
                follow_up = (
                    "以上是当前可用的数据源列表。请用友好的方式向用户展示有哪些数据源可用，"
                    "每个数据源列出名称、类型和关键字段，引导用户告诉你他想分析哪个数据源。"
                    "使用 ## 标题和列表格式，数字加粗。不要索要字段信息（你已经有了）。"
                )
            elif any(n == "render_chart" for n in executed_tool_names):
                follow_up = (
                    "图表已生成。现在请根据以上查询结果输出一份中文分析报告，"
                    "用 ## 标题分段，关键数字用 **加粗**，用 > 引用块展示重要发现。"
                    "**不要再查询了**，直接把报告输出给用户。"
                )
            elif any(n == "query_datasource" for n in executed_tool_names):
                if has_error:
                    if consecutive_query_failures == 1:
                        follow_up = (
                            "上次查询失败了。错误提示中已经包含了正确的 table_ref 和可用列名。"
                            "请**直接用错误提示中的 table_ref 作为 FROM 子句**，用错误提示中的列名拼写 SQL，不要自己编表名或列名。"
                            "然后调用 query_datasource 重试。"
                        )
                    elif consecutive_query_failures <= 3:
                        follow_up = (
                            f"已连续失败 {consecutive_query_failures} 次。请再次确认：\n"
                            "1) 列名是否完全等于 list_datasources 返回的 columns 数组中的字符串（区分大小写）；\n"
                            "2) FROM 子句是否就是 list_datasources 返回的 table_ref 原样复制；\n"
                            "3) 字符串值是否用了正确的引号。\n"
                            "如果仍然报错，请**最后一次**重试，再失败就把错误信息告诉用户并停止。"
                        )
                    else:
                        follow_up = (
                            "已连续失败多次，请停止重试，把最后一次的错误信息用中文告诉用户，"
                            "并建议用户检查数据源连接或简化查询条件。不要再继续重试。"
                        )
                else:
                    follow_up = (
                        "查询成功！现在请**立即**输出分析结果：\n"
                        "1. 先调用 render_chart 生成图表（**必须生成，不允许跳过**）\n"
                        "2. 然后输出中文分析报告，用 ## 标题分段，关键数字用 **加粗**\n"
                        "**禁止**再发起新的查询，直接用已有数据完成分析。"
                    )
            else:
                follow_up = (
                    "请根据以上工具执行结果输出回复。"
                    "用 ## 标题分段，关键数字用 **加粗**。"
                )

            messages.append({
                "role": "user",
                "content": (
                    f"以上工具已执行完毕，结果如下：\n{results_text}\n\n{follow_up}"
                ),
            })

        yield {"type": "done"}


def _fallback_clean(duckdb_stats: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in duckdb_stats or []:
        if not isinstance(s, dict):
            continue
        count = int(s.get("count") or 0)
        percentage = float(s.get("percentage") or 0.0)
        if count <= 0:
            continue
        severity = "high" if percentage >= 30 else ("medium" if percentage >= 5 else "low")
        issue_type = str(s.get("issue_type") or "missing")
        field = s.get("field")
        suggestion_map = {
            "missing": "建议使用默认值或剔除缺失行后重新聚合",
            "outlier": "建议使用 Z-Score 或 IQR 过滤异常值",
            "duplicate": "建议使用 DISTINCT 去重或保留首条",
            "format": "建议按正则表达式统一格式",
        }
        sample_raw = s.get("sample_values")
        out.append(
            {
                "field": field,
                "issue_type": issue_type,
                "suggestion": suggestion_map.get(issue_type, "建议人工复核"),
                "severity": severity,
                "count": count,
                "percentage": percentage,
                "sample": sample_raw if isinstance(sample_raw, list) else [],
            }
        )
    out.sort(key=lambda x: _SEV_ORDER.get(x.get("severity", "low"), 3))
    return out