"""Agent 评测评分逻辑。

四个核心指标：
1. SQL 准确率：执行 expected_sql 与 agent_sql，比较结果集是否一致
2. 工具调用成功率：是否调用了正确的工具
3. 平均迭代轮数：最终 done 之前的循环次数
4. 输出有效率：最终 response 是否非空且包含 expected_keywords

使用：
    from tests.agent_evals.judge import EvalResult, judge_attempt

    result = judge_attempt(question, attempt)
    print(result.summary())
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("lvco.agent_evals.judge")


@dataclass
class AttemptTrace:
    """单次 Agent 执行的完整轨迹，用于评分。"""

    question_id: str
    user_msg: str
    events: list[dict[str, Any]] = field(default_factory=list)
    final_response: str = ""
    error: str | None = None
    iteration_count: int = 0


@dataclass
class EvalResult:
    """单道题的评测结果。"""

    question_id: str
    sql_accuracy: bool = False       # SQL 准确率（与期望一致）
    tool_success: bool = False       # 工具调用成功（调用了正确工具且无 error）
    final_response_valid: bool = False  # 输出有效率
    chart_type_correct: bool = False  # 图表类型正确
    chart_config_valid: bool = False  # 图表配置合法（复用 validate_chart 校验器）
    chart_config_error: str = ""      # 校验失败原因
    iteration_count: int = 0         # 迭代轮数
    expected_chart_type: str = ""
    actual_chart_type: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def judge_attempt(
    question: dict[str, Any],
    attempt: AttemptTrace,
    *,
    sql_executor: Any | None = None,
) -> EvalResult:
    """对单次 Agent 执行进行评分。

    Args:
        question: dataset.jsonl 中的一道题（含 expected_sql_template / expected_chart_type / expected_keywords 等）
        attempt: 单次 Agent 执行的轨迹
        sql_executor: 可选，执行 SQL 的函数。传入时实际执行 expected_sql 与 agent_sql 比较结果；
                     不传入时仅做文本相似度判断。

    Returns:
        EvalResult：含四项指标
    """
    result = EvalResult(
        question_id=question.get("id", "?"),
        iteration_count=attempt.iteration_count,
        expected_chart_type=question.get("expected_chart_type", ""),
    )

    # ---- 1. 工具调用成功率 ----
    tool_events = [e for e in attempt.events if e.get("type") == "tool_call"]
    tool_results = [e for e in attempt.events if e.get("type") == "tool_result"]
    # "最终成功"模式：某工具只要有任意一次调用成功，就计为该工具成功。
    # Agent 自纠错场景：query_engine 失败 → 改用 query_datasource 成功，应计为成功
    tool_has_success: dict[str, bool] = {}
    for e in tool_results:
        name = e.get("name", "")
        res = e.get("result")
        is_err = (
            (isinstance(res, str) and '"error"' in res)
            or (isinstance(res, dict) and "error" in res)
        )
        if not is_err:
            tool_has_success[name] = True
    # 核心工具必须至少有一次成功调用
    query_ok = tool_has_success.get("query_datasource", False) or tool_has_success.get("query_engine", False)
    chart_ok = tool_has_success.get("render_chart", False)
    called_query = any(e.get("name") in ("query_datasource", "query_engine") for e in tool_events)
    result.tool_success = called_query and query_ok
    if not result.tool_success:
        result.notes.append(
            f"tool_calls={[e.get('name') for e in tool_events]} query_ok={query_ok}"
        )

    # ---- 2. SQL 准确率 ----
    agent_sql = _extract_agent_sql(attempt.events)
    if agent_sql and sql_executor:
        try:
            expected_rows = sql_executor(question["expected_sql_template"])
            actual_rows = sql_executor(agent_sql)
            result.sql_accuracy = _rows_match(expected_rows, actual_rows)
            if not result.sql_accuracy:
                result.notes.append(
                    f"result_set_mismatch: expected_rows={len(expected_rows)} actual_rows={len(actual_rows)}"
                )
        except Exception as e:
            result.notes.append(f"sql_executor_error={e}")
            result.sql_accuracy = _sql_keyword_overlap(
                question.get("expected_sql_template", ""),
                agent_sql,
            ) >= 0.6
    elif agent_sql:
        result.sql_accuracy = _sql_keyword_overlap(
            question.get("expected_sql_template", ""),
            agent_sql,
        ) >= 0.6
        if not result.sql_accuracy:
            result.notes.append("sql_keyword_overlap<0.6")

    # ---- 3. 输出有效率 ----
    keywords = question.get("expected_keywords") or []
    if keywords and attempt.final_response:
        result.final_response_valid = all(
            _keyword_present(kw, attempt.final_response) for kw in keywords
        )
    else:
        result.final_response_valid = bool(attempt.final_response and len(attempt.final_response) > 10)
    if not result.final_response_valid:
        result.notes.append(
            f"response_len={len(attempt.final_response)} keywords_missing={[kw for kw in keywords if not _keyword_present(kw, attempt.final_response)]}"
        )

    # ---- 4. 图表类型正确 ----
    chart_events = [e for e in attempt.events if e.get("type") == "chart"]
    if chart_events:
        result.actual_chart_type = chart_events[-1].get("chart_type", "")
        result.chart_type_correct = (
            result.actual_chart_type == result.expected_chart_type
        )
        if not result.chart_type_correct:
            result.notes.append(
                f"expected={result.expected_chart_type} actual={result.actual_chart_type}"
            )

    # ---- 5. 图表配置合法性（复用 validate_chart 共享校验器）----
    chart_tool_events = [
        e for e in attempt.events
        if e.get("type") == "tool_call" and e.get("name") == "render_chart"
    ]
    if chart_tool_events:
        args = chart_tool_events[-1].get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        try:
            from app.services.agent_tools import validate_chart_config, validate_chart_option

            if isinstance(args, dict) and (
                isinstance(args.get("option"), dict) or args.get("chart_type")
            ):
                vr = validate_chart_option(
                    args.get("chart_type"),
                    args.get("columns"),
                    args.get("rows"),
                    args.get("option"),
                )
            else:
                vr = validate_chart_config(args, None)
            result.chart_config_valid = bool(vr.get("valid"))
            if not vr.get("valid"):
                result.chart_config_error = "；".join(vr.get("errors") or [])
                result.notes.append(f"chart_config_invalid: {result.chart_config_error}")
        except Exception as e:
            result.notes.append(f"chart_validation_error={e}")

    return result


def aggregate_results(results: list[EvalResult]) -> dict[str, Any]:
    """汇总一批 EvalResult，输出 baseline 报告。"""
    if not results:
        return {
            "total": 0,
            "sql_accuracy_rate": 0.0,
            "tool_success_rate": 0.0,
            "final_response_valid_rate": 0.0,
            "chart_type_correct_rate": 0.0,
            "chart_config_valid_rate": 0.0,
            "avg_iterations": 0.0,
        }

    total = len(results)
    sql_ok = sum(1 for r in results if r.sql_accuracy)
    tool_ok = sum(1 for r in results if r.tool_success)
    resp_ok = sum(1 for r in results if r.final_response_valid)
    chart_ok = sum(1 for r in results if r.chart_type_correct)
    chart_cfg_ok = sum(1 for r in results if r.chart_config_valid)
    avg_iter = sum(r.iteration_count for r in results) / total

    return {
        "total": total,
        "sql_accuracy_rate": round(sql_ok / total, 4),
        "tool_success_rate": round(tool_ok / total, 4),
        "final_response_valid_rate": round(resp_ok / total, 4),
        "chart_type_correct_rate": round(chart_ok / total, 4),
        "chart_config_valid_rate": round(chart_cfg_ok / total, 4),
        "avg_iterations": round(avg_iter, 2),
        "sql_accuracy_count": sql_ok,
        "tool_success_count": tool_ok,
        "final_response_valid_count": resp_ok,
        "chart_type_correct_count": chart_ok,
        "chart_config_valid_count": chart_cfg_ok,
    }


# ----------------------------------------------------------------------
# 内部辅助函数
# ----------------------------------------------------------------------


def _extract_agent_sql(events: list[dict[str, Any]]) -> str:
    """从 agent 工具调用事件中提取最后一个有效的 query_datasource SQL。

    Agent 可能多次查询（探查→纠错→最终查询），取最后一个以反映最终结果。
    """
    last_sql = ""
    for e in events:
        if e.get("type") == "tool_call" and e.get("name") == "query_datasource":
            args = e.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    continue
            sql = args.get("sql") or args.get("query")
            if isinstance(sql, str) and sql.strip():
                last_sql = sql
    return last_sql


def _rows_match(expected: list, actual: list, *, tolerance: float = 1e-6) -> bool:
    """语义化结果集匹配：EXP 的每一行，其全部值都必须被 ACT 的某一行覆盖。

    设计目标：衡量"agent 是否答对了用户问题所需的信息"，而不是逐字节相等。
    - 列名 / 列顺序 / 列数无关（值包含：agent 多查 COUNT(*) 等列不影响）
    - datetime 与 date 归一化为 iso 字符串（date_trunc 返回 timestamp，直接取 date 等价）
    - 浮点数 round 4 位容忍
    - agent 多查行（LIMIT 更宽、按年分组、未筛选全量）不影响，只要 EXP 的行都被覆盖
    """
    if not expected and not actual:
        return True
    if not expected or not actual:
        return False

    def _norm(v: Any) -> Any:
        if isinstance(v, datetime.datetime):
            return v.date().isoformat()
        if isinstance(v, datetime.date):
            return v.isoformat()
        if isinstance(v, float):
            return round(v, 4)
        # '2025-08' 月份字符串 → '2025-08-01'，与 date_trunc('month') 对齐
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}", v):
            return v + "-01"
        return v

    def _row_vals(r: Any) -> list[Any]:
        if isinstance(r, dict):
            vals = list(r.values())
        elif isinstance(r, (list, tuple)):
            vals = list(r)
        else:
            vals = [r]
        return [_norm(v) for v in vals]

    act_sets = [set(_row_vals(r)) for r in actual]
    for er in expected:
        ev = set(_row_vals(er))
        if not any(ev <= aset for aset in act_sets):
            return False
    return True


def _normalize_row(row: Any) -> dict:
    if isinstance(row, dict):
        return {str(k): v for k, v in row.items()}
    if isinstance(row, (list, tuple)):
        return {str(i): v for i, v in enumerate(row)}
    return {"_": row}


def _row_key(row: dict) -> tuple:
    items = []
    for k in sorted(row.keys()):
        v = row[k]
        if isinstance(v, float):
            v = round(v, 4)
        items.append((k, v))
    return tuple(items)


def _row_value_key(row: Any) -> tuple:
    """行的值元组（忽略列名，保持 SELECT 顺序），用于结果集比较。"""
    if isinstance(row, dict):
        vals = list(row.values())
    elif isinstance(row, (list, tuple)):
        vals = list(row)
    else:
        vals = [row]
    out = []
    for v in vals:
        if isinstance(v, float):
            v = round(v, 4)
        out.append(v)
    return tuple(out)


def _sql_keyword_overlap(expected: str, actual: str) -> float:
    """比较两条 SQL 的关键字（SELECT/FROM/WHERE/GROUP BY/聚合函数/字段名）重叠度。"""
    keywords_a = set(_sql_keywords(expected))
    keywords_b = set(_sql_keywords(actual))
    if not keywords_a:
        return 1.0
    intersection = keywords_a & keywords_b
    return len(intersection) / len(keywords_a | keywords_b) if (keywords_a | keywords_b) else 0.0


_SQL_KEYWORDS = {
    "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT",
    "SUM", "AVG", "COUNT", "MAX", "MIN", "STDDEV", "MEDIAN",
    "COUNT_DISTINCT", "ASC", "DESC",
    "AND", "OR", "NOT", "IN", "BETWEEN", "LIKE", "IS", "NULL",
    "date_trunc", "now", "interval",
}


def _sql_keywords(sql: str) -> list[str]:
    """提取 SQL 中的关键字。"""
    if not sql:
        return []
    upper = sql.upper()
    found = []
    for kw in _SQL_KEYWORDS:
        if kw in upper:
            found.append(kw)
    # 提取引号内的字段名（"column_name"）
    fields = re.findall(r'"([^"]+)"', sql)
    found.extend(fields)
    return found


_CN2ARAB = {
    "零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}


def _norm_kw(s: str) -> str:
    """归一化关键词/文本：小写、去空白、中文数字转阿拉伯数字（含"前五"→"前5"）。"""
    s = (s or "").lower().replace(" ", "").replace("：", ":").replace("，", ",")
    for cn, ar in _CN2ARAB.items():
        s = s.replace(cn, ar)
    return s


# 同义词别名：关键词命中其任一别名也算命中（语义等价的不同表述）
_KW_ALIASES = {
    "每天": ["每日", "日均", "日销售额", "日销量"],
    "每日": ["每天", "日均", "日销售额", "日销量"],
    "分布": ["分段", "构成", "统计"],
    "金额": ["订单", "销售额", "价格"],
    "前10": ["top10", "前十名", "排名前10"],
    "前5": ["top5", "前五名", "排名前5"],
    "7天": ["七日", "一周"],
    "6个月": ["半年", "六个月", "近半年"],
}


def _keyword_present(keyword: str, text: str) -> bool:
    """判断关键字是否出现在文本中：忽略大小写/空白/中文数字差异，支持同义词别名。

    例如关键词"前10"可匹配"前 10"、"前十"、"Top 10"；"每天"可匹配"每日"。
    """
    ntext = _norm_kw(text)
    if _norm_kw(keyword) in ntext:
        return True
    for alias in _KW_ALIASES.get(keyword, []):
        if _norm_kw(alias) in ntext:
            return True
    return False