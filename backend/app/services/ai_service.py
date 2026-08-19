import asyncio
import json
import logging
import re
import time
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
from app.services.observability import (
    get_observer,
    observe_llm_call,
    observe_tool_call,
)

log = logging.getLogger("lvco.ai_service")


_SEV_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_INSIGHT_SEV_ORDER: dict[str, int] = {"warning": 0, "success": 1, "info": 2}

VALID_CHART_TYPES: frozenset[str] = frozenset({
    "bar", "line", "pie", "donut", "area", "scatter", "kpi_card",
    "grouped_bar", "stacked_bar", "horizontal_bar",
    "funnel", "heatmap", "radar", "sankey",
})


def _extract_json_array(text: str) -> list | None:
    """从 LLM 返回的文本中提取 JSON 数组，支持 ```json 代码块和裸 JSON 两种格式。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        解析成功返回 list，失败返回 None。
    """
    if not text:
        return None
    # 优先尝试匹配 ```json ... ``` 代码块中的 JSON 数组
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        # 回退：直接找文本中第一个 [ 和最后一个 ]
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
    """从 LLM 返回的文本中提取 JSON 对象，支持 ```json 代码块和裸 JSON 两种格式。

    Args:
        text: LLM 返回的原始文本。

    Returns:
        解析成功返回 dict，失败返回 None。
    """
    if not text:
        return None
    # 优先尝试匹配 ```json ... ``` 代码块中的 JSON 对象
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        # 回退：直接找文本中第一个 { 和最后一个 }
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
    """判断字段是否为时间/日期类型，通过字段类型名称或字段名称来识别。

    Args:
        field: 字段元信息字典，可能包含 name/field、type/data_type/dtype 等键。

    Returns:
        如果字段类型或名称包含时间相关关键字返回 True，否则返回 False。
    """
    if not isinstance(field, dict):
        return False
    name = str(field.get("name") or field.get("field") or "").lower()
    ftype = str(field.get("type") or field.get("data_type") or field.get("dtype") or "").lower()
    # 通过字段原始数据类型判断（如 date、timestamp 等）
    if any(token in ftype for token in ("date", "time", "timestamp", "datetime")):
        return True
    # 通过字段名称判断（如 month、year 等常见时间相关命名）
    if any(token in name for token in ("date", "time", "timestamp", "month", "year", "day")):
        return True
    return False


def _meta_lookup(field_meta: list[dict] | None) -> dict[str, dict]:
    """将字段元信息列表转换为以字段名为 key 的字典，便于快速查找。

    Args:
        field_meta: 字段元信息列表，每个元素为包含 name 或 field 键的字典。

    Returns:
        {字段名: 字段元信息字典} 的映射字典。
    """
    out: dict[str, dict] = {}
    for f in field_meta or []:
        if isinstance(f, dict):
            name = f.get("name") or f.get("field")
            if isinstance(name, str):
                out[name] = f
    return out


def _normalize_measures(current_config: dict) -> list[dict]:
    """规范化度量配置，将字符串或字典格式统一转换为 {field, agg} 字典列表。

    Args:
        current_config: 当前图表配置，可能包含 measures 字段。

    Returns:
        规范化后的度量列表，每个元素为 {"field": str, "agg": str}。
    """
    raw = current_config.get("measures") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for m in raw:
        if isinstance(m, str):
            # 简写格式："sales" → {"field": "sales", "agg": "SUM"}
            out.append({"field": m, "agg": "SUM"})
        elif isinstance(m, dict):
            # 完整格式：从 field/name/alias 中取字段名，聚合方式默认为 SUM
            field = m.get("field") or m.get("name") or m.get("alias")
            agg = m.get("agg") or "SUM"
            if isinstance(field, str):
                out.append({"field": field, "agg": agg})
    return out


def _normalize_dimensions(current_config: dict) -> list[str]:
    """规范化维度配置，将字符串或字典格式统一转换为字符串列表。

    Args:
        current_config: 当前图表配置，可能包含 dimensions 字段。

    Returns:
        规范化后的维度字段名列表。
    """
    raw = current_config.get("dimensions") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for d in raw:
        if isinstance(d, str):
            out.append(d)
        elif isinstance(d, dict):
            # 字典格式：从 field/name/alias 中取字段名
            name = d.get("field") or d.get("name") or d.get("alias")
            if isinstance(name, str):
                out.append(name)
    return out


def _confidence_for(rule: str) -> float:
    """根据规则名称返回推荐的置信度分数。

    Args:
        rule: 规则名称，如 "kpi"、"time_line"、"grouped" 等。

    Returns:
        对应的置信度分数，范围 0.0 - 1.0，未知规则返回默认值 0.7。
    """
    return {
        "kpi": 0.9,
        "time_line": 0.85,
        "grouped": 0.82,
        "single_dim": 0.78,
        "default": 0.7,
    }.get(rule, 0.7)


def _build_config(current_config: dict, chart_type: str) -> dict:
    """在现有配置基础上注入图表类型，构建完整配置字典。

    Args:
        current_config: 当前的图表配置字典。
        chart_type: 目标图表类型（如 "bar"、"line"、"kpi_card"）。

    Returns:
        注入 chartType 和 chart_type 后的新配置字典。
    """
    cfg = dict(current_config or {})
    # 同时写入驼峰和下划线两种命名，兼容不同消费方
    cfg["chartType"] = chart_type
    cfg["chart_type"] = chart_type
    return cfg


class AIService:
    """AI 服务，封装与 LLM 交互的各类业务逻辑，包括聊天、图表推荐、数据清洗建议、洞察生成、文本润色及 Agent 循环。"""

    def __init__(self, llm: LLMClient | None = None, settings_override: Settings | None = None) -> None:
        """初始化 AIService 实例。

        Args:
            llm: LLM 客户端实例，未传入时根据配置自动创建。
            settings_override: 可选的配置覆盖，未传入时使用全局配置。
        """
        cfg = settings_override or settings
        self.llm = llm or LLMClient(cfg)

    async def chat_stream(
        self,
        history: list[dict[str, str]],
        user_msg: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[str]:
        """流式聊天对话，将用户消息和历史记录拼接后调用 LLM 流式接口。

        Args:
            history: 历史对话列表，每个元素为 {"role": ..., "content": ...}。
            user_msg: 当前用户输入的消息文本。
            user_id: 当前用户 ID，用于 Langfuse trace 关联。
            session_id: 会话 ID，用于 Langfuse trace 关联。

        Yields:
            LLM 返回的逐 token 文本片断。
        """
        start_time = time.time()
        log.info(f"[chat_stream] start user_id={user_id} session_id={session_id} "
                f"msg_length={len(user_msg)} history_count={len(history or [])}")
        
        messages: list[dict[str, str]] = [{"role": "system", "content": CHAT_SYSTEM}]
        for h in history or []:
            if not isinstance(h, dict):
                continue
            role = h.get("role")
            content = h.get("content")
            # 只保留 user 和 assistant 角色的消息，跳过无效消息
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_msg})
        
        log.debug(f"[chat_stream] messages_prepared count={len(messages)} "
                 f"total_length={sum(len(m.get('content', '')) for m in messages)}")

        observer = get_observer()
        with observer.trace(
            "chat_stream",
            user_id=user_id,
            session_id=session_id,
            metadata={"message_count": len(messages)},
        ) as trace:
            with observe_llm_call(
                trace,
                "chat_stream",
                messages=messages,
                model=settings.model_for_task("simple"),
                temperature=0.5,
            ) as span:
                collected: list[str] = []
                async for token in self.llm.stream_chat(messages, temperature=0.5):
                    collected.append(token)
                    yield token
                output_text = "".join(collected)
                elapsed_ms = int((time.time() - start_time) * 1000)
                span.update(output=output_text, metadata={"token_count": len(collected)})
                log.info(f"[chat_stream] complete user_id={user_id} elapsed_ms={elapsed_ms} "
                        f"token_count={len(collected)} output_length={len(output_text)}")

    async def recommend_charts(
        self,
        field_meta: list[dict],
        current_config: dict,
        user_id: str | None = None,
    ) -> list[dict]:
        """推荐合适的图表类型。优先尝试 LLM AI 推荐，失败时回退到基于规则的推荐。

        Args:
            field_meta: 字段元信息列表。
            current_config: 当前图表配置，包含 measures、dimensions 等。
            user_id: 当前用户 ID，用于注入用户偏好。

        Returns:
            图表推荐列表，每个元素包含 chart_type、rationale、config、confidence 等字段。
        """
        start_time = time.time()
        dimensions = _normalize_dimensions(current_config)
        measures = _normalize_measures(current_config)
        
        log.info(f"[recommend_charts] start field_count={len(field_meta or [])} "
                f"dimension_count={len(dimensions)} measure_count={len(measures)} "
                f"user_id={user_id}")

        if not measures:
            log.warning("[recommend_charts] no_measures_provided")
            raise ValueError("请至少添加一个度量")

        # 获取用户偏好（如果提供了 user_id）
        user_preferences = None
        if user_id:
            try:
                from app.repositories.user_preference_repository import (
                    SQLAlchemyUserPreferenceRepository,
                )
                from app.services.user_preference_service import UserPreferenceService
                from app.core.database import async_session

                async with async_session() as db:
                    pref_repo = SQLAlchemyUserPreferenceRepository(db)
                    pref_service = UserPreferenceService(user_preference_repo=pref_repo)
                    user_preferences = await pref_service.get_user_preferences(user_id)
                    log.info(f"[recommend_charts] loaded {len(user_preferences)} user preferences")
            except Exception as e:
                log.warning(f"[recommend_charts] failed to load user preferences: {e}")

        # 先尝试调用 LLM 给真正的 AI 推荐，失败时回退到规则匹配
        log.debug("[recommend_charts] attempting_llm_recommendation")
        llm_suggestions = await self._recommend_charts_llm(
            field_meta, current_config, user_preferences=user_preferences
        )
        if llm_suggestions:
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.info(f"[recommend_charts] llm_success count={len(llm_suggestions)} elapsed_ms={elapsed_ms}")
            return llm_suggestions

        # 回退：基于规则的快速推荐
        log.info("[recommend_charts] llm_failed_or_empty fallback_to_rules")
        rules_result = self._recommend_charts_by_rules(field_meta, current_config)
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.info(f"[recommend_charts] rules_fallback count={len(rules_result)} elapsed_ms={elapsed_ms}")
        return rules_result

    async def _recommend_charts_llm(
        self,
        field_meta: list[dict],
        current_config: dict,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        user_preferences: list | None = None,
    ) -> list[dict]:
        """调用 LLM 生成图表推荐。LLM 不可用或返回无效时返回空列表，由调用方回退到规则。

        Args:
            field_meta: 字段元信息列表。
            current_config: 当前图表配置。
            user_id: 当前用户 ID，用于 Langfuse trace 关联。
            session_id: 会话 ID，用于 Langfuse trace 关联。
            user_preferences: 用户偏好列表，用于注入到 prompt。

        Returns:
            有效的图表推荐列表（已校验 chart_type 合法性），空列表表示需回退到规则推荐。
        """
        start_time = time.time()
        log.debug(f"[_recommend_charts_llm] start field_count={len(field_meta or [])} "
                 f"config_keys={list((current_config or {}).keys())} "
                 f"has_preferences={user_preferences is not None}")
        
        try:
            self.llm._check_configured()
        except AINotConfiguredError:
            log.info("[_recommend_charts_llm] llm_not_configured fallback_to_rules")
            return []

        observer = get_observer()
        with observer.trace(
            "recommend_charts",
            user_id=user_id,
            session_id=session_id,
            metadata={"field_count": len(field_meta or []), "config_keys": list((current_config or {}).keys())},
        ) as trace:
            try:
                # 构建用户偏好提示词
                preference_hint = ""
                if user_preferences:
                    preference_hint = "\n\n## 用户偏好\n"
                    for pref in user_preferences[:5]:  # 最多取5个偏好
                        pref_type = pref.get("preference_type", "")
                        pref_key = pref.get("preference_key", "")
                        pref_value = pref.get("preference_value", "")
                        if pref_type == "chart_type":
                            preference_hint += f"- 用户偏好使用 {pref_value} 类型图表\n"
                        elif pref_type == "color_scheme":
                            preference_hint += f"- 用户偏好 {pref_value} 配色方案\n"
                        elif pref_type == "dimension":
                            preference_hint += f"- 用户偏好以 {pref_value} 作为维度\n"
                    log.info(f"[_recommend_charts_llm] injected {len(user_preferences)} user preferences into prompt")
                
                messages = [
                    {"role": "system", "content": RECOMMEND_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            "## 字段元信息\n"
                            + json.dumps(field_meta or [], ensure_ascii=False, default=str)
                            + "\n\n## 当前配置\n"
                            + json.dumps(current_config or {}, ensure_ascii=False, default=str)
                            + preference_hint
                            + "\n\n请基于以上字段、当前配置和用户偏好，推荐 1-3 个最合适的图表类型，"
                            "并返回严格 JSON 数组。"
                        ),
                    },
                ]
                
                prompt_length = sum(len(m.get("content", "")) for m in messages)
                log.debug(f"[_recommend_charts_llm] prompt_built length={prompt_length}")
                
                with observe_llm_call(
                    trace,
                    "recommend_charts",
                    messages=messages,
                    model=settings.model_for_task("recommend"),
                    temperature=0.4,
                ) as span:
                    content = await self.llm.complete(messages, temperature=0.4, max_tokens=800)
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    span.update(output=content, metadata={"output_length": len(content)})
                    log.debug(f"[_recommend_charts_llm] llm_response_received length={len(content)} elapsed_ms={elapsed_ms}")
                
                suggestions = _extract_json_array(content) or []
                log.debug(f"[_recommend_charts_llm] json_extracted raw_count={len(suggestions)}")
                
                normalized: list[dict] = []
                for s in suggestions:
                    if not isinstance(s, dict):
                        continue
                    ct = s.get("chart_type") or s.get("chartType")
                    if not isinstance(ct, str):
                        continue
                    # 校验 LLM 返回的图表类型是否在合法列表中，不在则跳过
                    if ct not in VALID_CHART_TYPES:
                        log.warning("[_recommend_charts_llm] invalid_chart_type type=%s valid_types=%s", 
                                   ct, list(VALID_CHART_TYPES))
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
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    log.info("[_recommend_charts_llm] success count=%d elapsed_ms=%d chart_types=%s", 
                            len(normalized), elapsed_ms, [n["chart_type"] for n in normalized])
                    trace.metadata["suggestion_count"] = len(normalized)
                    return normalized
                log.warning("[_recommend_charts_llm] no_valid_suggestions_extracted raw_count=%d", len(suggestions))
                return []
            except (AINotConfiguredError, AIUpstreamError) as e:
                elapsed_ms = int((time.time() - start_time) * 1000)
                log.warning("[_recommend_charts_llm] llm_error error=%s elapsed_ms=%d fallback_to_rules", e, elapsed_ms)
                return []
            except Exception as e:  # noqa: BLE001
                elapsed_ms = int((time.time() - start_time) * 1000)
                log.exception("[_recommend_charts_llm] unexpected_error error=%s elapsed_ms=%d fallback_to_rules", e, elapsed_ms)
                return []

    def _recommend_charts_by_rules(
        self,
        field_meta: list[dict],
        current_config: dict,
    ) -> list[dict]:
        """基于硬编码规则快速推荐图表，无需 LLM。根据维度数量和是否含时间字段选择图表。

        Args:
            field_meta: 字段元信息列表。
            current_config: 当前图表配置。

        Returns:
            图表推荐列表，按适用场景给出 1-3 个候选项。
        """
        dimensions = _normalize_dimensions(current_config)
        measures = _normalize_measures(current_config)
        meta = _meta_lookup(field_meta)
        # 检查是否存在时间维度字段
        time_dim = next(
            (d for d in dimensions if _is_time_like(meta.get(d))),
            None,
        )

        # 场景一：无维度，仅有度量 → KPI 卡片
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

        # 场景二：含时间维度 + 单度量 → 折线图为主，柱状图和面积图为辅
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

        # 场景三：单维度 + 多度量 → 分组柱状图为主
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

        # 场景四：兜底 — 任意维度度量组合
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
        """调用 LLM 生成数据清洗建议。LLM 不可用时回退到基于统计的规则清洗。

        Args:
            field_meta: 字段元信息列表。
            duckdb_stats: DuckDB 统计信息列表，包含缺失值、异常值、重复等统计。

        Returns:
            清洗建议列表，按 severity（high > medium > low）排序。
        """
        start_time = time.time()
        log.info(f"[clean_suggest] start field_count={len(field_meta or [])} "
                f"stats_count={len(duckdb_stats or [])}")
        
        prompt_user = (
            "Field meta: "
            + json.dumps(field_meta or [], ensure_ascii=False, default=str)
            + "\nDuckDB stats: "
            + json.dumps(duckdb_stats or [], ensure_ascii=False, default=str)
            + "\n请给出清洗建议，按 severity 高→低排序。"
        )
        
        log.debug(f"[clean_suggest] prompt_built length={len(prompt_user)}")

        try:
            content = await self.llm.complete(
                [
                    {"role": "system", "content": CLEAN_SYSTEM},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=0.2,
            )
            log.debug(f"[clean_suggest] llm_response_received length={len(content)}")
        except (AINotConfiguredError, AIUpstreamError) as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.warning("[clean_suggest] llm_error error=%s elapsed_ms=%d fallback_to_rules", e, elapsed_ms)
            return _fallback_clean(duckdb_stats)

        # 优先解析 JSON 数组；如果返回的是对象则尝试取其中的 issues 字段
        raw_list = _extract_json_array(content)
        if raw_list is None:
            parsed_obj = _extract_json_object(content)
            if isinstance(parsed_obj, dict):
                inner = parsed_obj.get("issues")
                if isinstance(inner, list):
                    raw_list = inner
        if raw_list is None:
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.warning("[clean_suggest] json_parse_failed elapsed_ms=%d fallback_to_rules", elapsed_ms)
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
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.info(f"[clean_suggest] complete issue_count={len(issues)} elapsed_ms={elapsed_ms} "
                f"severity_distribution={ {s: sum(1 for i in issues if i.get('severity') == s) for s in ['high', 'medium', 'low']} }")
        return issues

    async def generate_insights(
        self,
        agg_result: list[dict],
        query_config: dict,
    ) -> list[dict]:
        """基于聚合查询结果调用 LLM 生成数据洞察，包含 TOP N 摘要以引导 LLM 关注关键数据。

        Args:
            agg_result: 聚合查询的结果数据列表。
            query_config: 查询配置，包含维度、度量等信息。

        Returns:
            洞察列表，每个元素包含 type、title、description、severity、related_fields。
        """
        start_time = time.time()
        top_rows = list(agg_result or [])[:200]

        log.info("[generate_insights] start row_count=%d first_row=%s",
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
        
        log.debug(f"[generate_insights] prompt_built length={len(prompt_user)} summary_lines_count={len(summary_lines)}")

        try:
            content = await self.llm.complete(
                [
                    {"role": "system", "content": INSIGHTS_SYSTEM},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=0.4,
                max_tokens=2000,
            )
            log.debug(f"[generate_insights] llm_response_received length={len(content)}")
        except (AINotConfiguredError, AIUpstreamError) as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.warning("[generate_insights] llm_error error=%s elapsed_ms=%d fallback_empty", e, elapsed_ms)
            return []

        # 优先解析 JSON 数组；如果返回的是对象则尝试取其中的 insights 字段
        raw_list = _extract_json_array(content)
        if raw_list is None:
            parsed_obj = _extract_json_object(content)
            if isinstance(parsed_obj, dict):
                inner = parsed_obj.get("insights")
                if isinstance(inner, list):
                    raw_list = inner
        if not isinstance(raw_list, list):
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.warning("[generate_insights] json_parse_failed elapsed_ms=%d return_empty", elapsed_ms)
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
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        log.info(f"[generate_insights] complete insight_count={len(insights)} elapsed_ms={elapsed_ms} "
                f"severity_distribution={ {s: sum(1 for i in insights if i.get('severity') == s) for s in ['warning', 'success', 'info']} }")
        return insights

    async def polish_text(self, text: str, style: str) -> dict:
        """调用 LLM 对文本进行润色，按指定风格输出润色后的版本。

        Args:
            text: 待润色的原始文本。
            style: 润色风格（如 "formal"、"concise"、"friendly" 等）。

        Returns:
            包含 original、polished、style 的字典。
        """
        start_time = time.time()
        log.info(f"[polish_text] start style={style} text_length={len(text)}")
        
        prompt_user = (
            f"Style: {style}\n"
            f"Original text: {text}\n"
            "请润色以上文本，按指定风格输出 polished 字段。"
        )
        
        log.debug(f"[polish_text] prompt_built length={len(prompt_user)}")

        try:
            content = await self.llm.complete(
                [
                    {"role": "system", "content": POLISH_SYSTEM},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=0.5,
            )
            log.debug(f"[polish_text] llm_response_received length={len(content)}")
        except (AINotConfiguredError, AIUpstreamError) as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            log.warning("[polish_text] llm_error error=%s elapsed_ms=%d fallback_original", e, elapsed_ms)
            polished = text
            parsed_obj: dict | None = None
        else:
            parsed_obj = _extract_json_object(content)
            # 取 LLM 返回的 polished 字段，若不存在则用原始响应文本
            polished_raw = parsed_obj.get("polished") if isinstance(parsed_obj, dict) else None
            polished = polished_raw if isinstance(polished_raw, str) and polished_raw else content.strip()

        elapsed_ms = int((time.time() - start_time) * 1000)
        log.info(f"[polish_text] complete style={style} elapsed_ms={elapsed_ms} "
                f"original_length={len(text)} polished_length={len(polished)}")
        
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
        """单 Agent 工具调用模式：Phase 状态机（SELECTING→ANALYZING→GENERATING→REPORTING）+ ToolRegistry。

        Agent 通过工具调用完成任务（list_datasources → query_datasource/query_engine → render_chart），
        工具内嵌自校验（L3 防护 + 图表配置校验），失败信息回传 LLM 自纠错。

        Args:
            user_id: 当前用户 ID。
            user_msg: 用户输入的查询或指令文本。
            history: 历史对话列表。
            db_session: 数据库会话对象，用于执行查询等操作。
            initial_phase: 初始阶段名称，默认为 "selecting"。

        Yields:
            事件字典，支持以下类型：
            - {"type": "text", "content": "..."} — LLM 生成的文本片段
            - {"type": "tool_call", "name": "...", "args": {...}} — 工具调用请求
            - {"type": "tool_result", "name": "...", "result": "..."} — 工具执行结果
            - {"type": "chart", "chart_type": "...", "option": {...}} — 图表渲染选项
            - {"type": "done"} — Agent 执行完成
            - {"type": "error", "message": "..."} — 执行出错
        """
        from app.services.agent_tools import ToolRegistry, ConversationPhase, get_tools_for_phase
        from app.services.ai_prompts import AGENT_SYSTEM
        from app.services.sql_guard import sql_guard, GuardResult

        log.info(f"[agent_stream] start user_id={user_id} phase={initial_phase} msg_length={len(user_msg)}")

        # L1 + L2: 安全检查 — SQL 注入、危险命令等
        guard_result: GuardResult = sql_guard.full_check(user_msg)
        if not guard_result.allowed:
            log.warning(f"[agent_stream] security_check_failed reason={guard_result.reason}")
            yield {"type": "error", "message": guard_result.reason}
            return

        # ── 编排模式：复杂任务走规划-执行编排器（AgentOrchestrator）──
        # 简单任务（短消息/列数据源）走下方单 Agent ReAct 状态机
        use_orchestrator = (
            settings.AGENT_ORCHESTRATOR_ENABLED
            and initial_phase == "selecting"
            and len(user_msg) > 20
            and not user_msg.strip().startswith("列出")
            and not user_msg.strip().startswith("有哪些")
        )
        if use_orchestrator:
            log.info("[agent_stream] using_orchestrator_mode")
            yield {"type": "status", "message": "正在启动多工具编排..."}

            # 获取可用数据源列表（供 Planner 规划时引用真实 datasource_id）
            available_datasources = []
            try:
                import uuid
                from app.repositories.datasource_repository import (
                    SQLAlchemyDataSourceRepository,
                )
                ds_repo = SQLAlchemyDataSourceRepository(db_session)
                user_uuid = uuid.UUID(user_id)
                datasources, _ = await ds_repo.list_datasources(user_uuid, page=1, page_size=100, source_type=None, status=None, search=None)
                from app.services.agent_tools import duckdb_client
                from app.models.datasource import SourceType
                for ds in datasources:
                    schema_meta = ds.schema_meta or {}
                    fields = schema_meta.get("fields", []) if isinstance(schema_meta, dict) else []
                    # 构建 table_ref，让 Planner/Executor 知道 FROM 后面写什么
                    conn_cfg = dict(ds.connection_config) if ds.connection_config else {}
                    schema_name = duckdb_client.get_schema_name(user_id, str(ds.id), ds.name, db_name=conn_cfg.get("db_name", ""))
                    if ds.source_type in (SourceType.postgresql, SourceType.mysql):
                        table_name = schema_meta.get("table_name", "data") if isinstance(schema_meta, dict) else "data"
                        table_ref = f'"{schema_name}".public."{table_name}"'
                    else:
                        table_ref = f'"{schema_name}"."data"'
                    available_datasources.append({
                        "id": str(ds.id),
                        "name": ds.name,
                        "description": ds.description,
                        "type": ds.source_type.value if ds.source_type else "unknown",
                        "fields": fields,
                        "table_ref": table_ref,
                    })
                log.info(f"[agent_stream] loaded_datasources count={len(available_datasources)}")
            except ValueError as e:
                log.error(f"[agent_stream] load_datasources_failed invalid_user_id: {user_id}")
            except Exception as e:
                log.error(f"[agent_stream] load_datasources_failed error={e}")

            # 规划-执行编排：Planner 动态规划 → Executor 按计划执行工具
            # ⚙️ 降级策略：异常时自动 fallback 到单 Agent ReAct 模式
            try:
                from app.services.agents import AgentOrchestrator
                orchestrator = AgentOrchestrator(self.llm, db_session)
                async for event in orchestrator.execute_task(
                    user_msg=user_msg,
                    history=history or [],
                    user_id=user_id,
                    available_datasources=available_datasources,
                ):
                    yield event
                return
            except Exception as e:
                log.warning(
                    f"[agent_stream] orchestrator_failed_fallback_to_single reason={e}",
                    exc_info=True,
                )
                yield {"type": "status", "message": "编排执行失败，自动回退到标准模式"}
                # 不 return，继续走单 Agent ReAct 路径

        # 单 Agent 模式：图化 ReAct（ReactGraphAgent，reason → execute_tools 循环）
        log.info(f"[agent_stream] using_single_agent_mode")
        from app.services.agent_tools import ToolRegistry, ConversationPhase, get_tools_for_phase
        from app.services.ai_prompts import AGENT_SYSTEM
        from app.services.agents.react_agent import ReactGraphAgent

        all_tools = ToolRegistry.schemas()

        observer = get_observer()
        with observer.trace(
            "agent_stream_single",
            user_id=user_id,
            session_id=getattr(db_session, "session_id", None) if db_session else None,
            metadata={
                "initial_phase": initial_phase,
                "history_length": len(history or []),
                "user_msg_length": len(user_msg),
            },
        ) as agent_trace:

            from app.services.context_utils import compress_history
            messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM}]
            # 历史消息压缩：保留最近 20 条；总长超限时把最早部分折叠为摘要行
            for h in (history or [])[-20:]:
                if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                    messages.append({"role": h["role"], "content": str(h.get("content", ""))})
            messages = compress_history(messages, keep=20, max_chars=8000)
            messages.append({"role": "user", "content": guard_result.sanitized_input or user_msg})

            # 图化 ReAct：由 ReactGraphAgent 内部图引擎执行（LLM 推理 → 工具执行 → 循环）
            # 事件经 asyncio.Queue 桥接保持流式输出，行为与原循环等价
            queue: asyncio.Queue = asyncio.Queue()

            async def emit(ev: dict) -> None:
                await queue.put(ev)

            async def run_react() -> None:
                try:
                    react = ReactGraphAgent(self.llm, all_tools, agent_trace)
                    await react.run(
                        messages=messages,
                        user_id=user_id,
                        db_session=db_session,
                        initial_phase=initial_phase,
                        emit=emit,
                    )
                except Exception as e:
                    log.exception(f"[agent_stream] react_graph_failed: {e}")
                    await emit({"type": "error", "message": f"Agent 执行异常: {str(e)}"})
                finally:
                    await queue.put(None)

            task = asyncio.create_task(run_react())
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield ev
            await task

        yield {"type": "done"}

def _fallback_clean(duckdb_stats: list[dict]) -> list[dict]:
    """基于 DuckDB 统计信息生成数据清洗建议的兜底方案，在 LLM 不可用时使用。

    根据每列的缺失率/异常率/重复率等统计值，按预设规则生成清洗建议并排序。

    Args:
        duckdb_stats: DuckDB 统计信息列表，每个元素包含 field、count、percentage、issue_type、sample_values 等。

    Returns:
        清洗建议列表，按 severity 排序，每个元素包含 field、issue_type、suggestion、severity、count、percentage、sample。
    """
    out: list[dict] = []
    for s in duckdb_stats or []:
        if not isinstance(s, dict):
            continue
        count = int(s.get("count") or 0)
        percentage = float(s.get("percentage") or 0.0)
        if count <= 0:
            continue
        # 根据百分比决定严重程度：>=30% 为 high，>=5% 为 medium，其余为 low
        severity = "high" if percentage >= 30 else ("medium" if percentage >= 5 else "low")
        issue_type = str(s.get("issue_type") or "missing")
        field = s.get("field")
        # 不同类型问题对应不同的建议文案
        suggestion_map = {
            "missing": "建议使用默认值或剔除缺失行后重新聚合",
            "outlier": "建议使用 Z-Score 或 IQR 过滤异常值",
            "outlier_iqr": "IQR 法检测到异常值，建议标记或剔除",
            "duplicate": "建议使用 DISTINCT 去重或保留首条",
            "format": "建议按正则表达式统一格式",
            "type_mismatch": "字段类型不一致，建议统一数据类型",
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
    # 按 severity 排序：high → medium → low
    out.sort(key=lambda x: _SEV_ORDER.get(x.get("severity", "low"), 3))
    return out
