# -*- coding: utf-8 -*-
"""工具契约（P3-A）：统一错误码 + 结构化错误 + 高频工具参数校验。

为什么需要这三件事
------------------
工具失败以前只有一句自由文本 {"error": "..."}：LLM 只能靠读中文猜怎么改，
人只能 grep 日志；agent_calls 的 error_kind 也只能靠关键词猜。
本次把它升级成**契约**：
  1) ErrorCode：有限的失败枚举；
  2) tool_error()：构造 {"error", "code", "retryable", "hint"}。**保留 "error" 键**
     （ToolExecutor.is_error_result 与上游编排都依赖它，向后兼容）；
  3) 高频工具 pydantic 入参模型：调用前 model_validate，失败返回 INVALID_ARGS +
     **字段路径**（例：limit: 需要整数，收到 "top10"），把"报错"变成"可执行的修正指令"。

error_kind 与 agent_calls 的表契约
----------------------------------
agent_calls.error_kind 的既有取值（P1-C 定稿）是：
    whitelist / unknown_tool / sensitive_column / query_timeout / tool_error
本次**不改这个集合**。JSON 里的 code 用大写枚举值（如 TOOL_TIMEOUT），
落库的 error_kind 通过 CODE_TO_ERROR_KIND 统一映射回既有取值：
    WHITELIST -> whitelist        UNKNOWN_TOOL -> unknown_tool
    SENSITIVE_COLUMN -> sensitive_column
    TIMEOUT -> query_timeout      TOOL_EXCEPTION -> tool_error
    INVALID_ARGS / NOT_FOUND / UPSTREAM -> tool_error
（INVALID_ARGS 若想独立成类，只需改 CODE_TO_ERROR_KIND 一行 + 迁移注释；
  代价是扩展 agent_calls 的取值集合，故默认不扩。）

通用约定
--------
- 参数模型一律 extra="ignore"：工具 execute 多收 **kwargs，且执行器会注入
  canvas_id 等上下文参数；校验只针对 LLM 给的显式参数，多余键忽略不算错。
- 必填性只在"工具自身没有友好缺参提示"的地方强制（query_sql/list_fields/
  query_engine）；render_chart / add_chart_block 保留各自更丰富的缺参引导，
  模型只校验类型，避免把它们的提示覆盖成通用报错。
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ErrorCode(str, Enum):
    """工具失败的有限枚举（JSON 的 code 字段 / 统计的主键）。"""

    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    WHITELIST = "WHITELIST"
    INVALID_ARGS = "INVALID_ARGS"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TOOL_TIMEOUT"
    TOOL_EXCEPTION = "TOOL_EXCEPTION"
    SENSITIVE_COLUMN = "SENSITIVE_COLUMN"
    UPSTREAM = "UPSTREAM"


# 每种错误的默认提示（LLM 自纠错用；调用方可覆盖）
DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.UNKNOWN_TOOL: "工具名不在可用工具表里，请只调用系统提供的工具",
    ErrorCode.WHITELIST: "该工具在当前入口不可用，请改用允许的工具",
    ErrorCode.INVALID_ARGS: "按参数说明修正类型与必填项后重试",
    ErrorCode.NOT_FOUND: "未找到目标资源，请先确认数据源/字段/会话是否存在",
    ErrorCode.TIMEOUT: "缩小查询范围或加筛选条件后重试",
    ErrorCode.TOOL_EXCEPTION: "工具内部异常；可换用其他工具，反复出现请告知用户",
    ErrorCode.SENSITIVE_COLUMN: "该列被标记为敏感，请改用其他字段，或让用户取消敏感标记",
    ErrorCode.UPSTREAM: "上游服务暂不可用，稍后重试",
}

# 是否值得让 LLM/上层重试（供编排策略与未来 repair_policy 使用）
DEFAULT_RETRYABLE: dict[ErrorCode, bool] = {
    ErrorCode.UNKNOWN_TOOL: False,
    ErrorCode.WHITELIST: False,
    ErrorCode.INVALID_ARGS: True,
    ErrorCode.NOT_FOUND: False,
    ErrorCode.TIMEOUT: True,
    ErrorCode.TOOL_EXCEPTION: False,
    ErrorCode.SENSITIVE_COLUMN: False,
    ErrorCode.UPSTREAM: True,
}

# agent_calls.error_kind 的既有取值（表契约，不要扩）
EXISTING_ERROR_KINDS: frozenset[str] = frozenset({
    "whitelist", "unknown_tool", "sensitive_column", "query_timeout", "tool_error",
})

# code -> 落库 error_kind（见模块头：默认不扩取值集合）
CODE_TO_ERROR_KIND: dict[ErrorCode, str] = {
    ErrorCode.UNKNOWN_TOOL: "unknown_tool",
    ErrorCode.WHITELIST: "whitelist",
    ErrorCode.INVALID_ARGS: "tool_error",       # 新增类别，但不扩表取值
    ErrorCode.NOT_FOUND: "tool_error",
    ErrorCode.TIMEOUT: "query_timeout",
    ErrorCode.TOOL_EXCEPTION: "tool_error",
    ErrorCode.SENSITIVE_COLUMN: "sensitive_column",
    ErrorCode.UPSTREAM: "tool_error",
}


def tool_error(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool | None = None,
    hint: str | None = None,
) -> str:
    """构造结构化错误 JSON（**保留 "error" 键**，向后兼容）。

    Args:
        code: ErrorCode（也接受其字符串值）
        message: 一句话说明"哪里错了"
        retryable: 是否值得重试；缺省按 code 的默认值
        hint: 给 LLM 的修正建议；缺省用 code 的默认提示
    """
    try:
        ec = code if isinstance(code, ErrorCode) else ErrorCode(str(code))
    except ValueError:
        ec = ErrorCode.TOOL_EXCEPTION
    payload: dict[str, Any] = {
        "error": str(message),
        "code": ec.value,
        "retryable": DEFAULT_RETRYABLE[ec] if retryable is None else bool(retryable),
    }
    h = hint if hint is not None else DEFAULT_HINTS.get(ec)
    if h:
        payload["hint"] = h
    return json.dumps(payload, ensure_ascii=False)


def error_kind_for(code: ErrorCode | str | None) -> str | None:
    """把 ErrorCode（或其字符串）映射成 agent_calls.error_kind 的既有取值。

    未知 code → None（调用方回退到文本分类）。
    """
    if code is None:
        return None
    try:
        ec = code if isinstance(code, ErrorCode) else ErrorCode(str(code))
    except ValueError:
        return None
    return CODE_TO_ERROR_KIND.get(ec)


def _zh_message(err: dict) -> str:
    """pydantic 错误类型 → 中文说明（LLM 与用户都读得懂）。"""
    etype = str(err.get("type") or "")
    ctx = err.get("ctx") or {}
    if etype == "missing":
        return "缺少必填字段"
    if etype in ("int_parsing", "int_type", "int_from_float"):
        return "需要整数"
    if etype in ("float_parsing", "float_type"):
        return "需要数字"
    if etype in ("bool_parsing", "bool_type"):
        return "需要布尔值"
    if etype in ("string_type",):
        return "需要字符串"
    if etype in ("list_type",):
        return "需要数组"
    if etype in ("dict_type",):
        return "需要对象"
    if etype in ("string_too_short", "too_short"):
        return "不能为空"
    if etype in ("string_too_long", "too_long"):
        return "过长"
    if etype == "greater_than_equal":
        return f"需大于等于 {ctx.get('ge')}"
    if etype == "less_than_equal":
        return f"需小于等于 {ctx.get('le')}"
    if etype == "extra_forbidden":
        return "不支持的参数"
    return str(err.get("msg") or "参数不合法")


def _fmt_value(value: Any, limit: int = 40) -> str:
    """把错误输入渲染成短字符串（字符串用 JSON 引号，便于对照）。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_validation_error(exc: ValidationError, *, max_items: int = 3) -> str:
    """把 pydantic ValidationError 渲染成「字段路径: 说明，收到 值」的多行文本。"""
    parts: list[str] = []
    for err in exc.errors()[:max_items]:
        loc = err.get("loc") or ()
        path = ".".join(str(item) for item in loc) or "参数"
        parts.append(f"{path}: {_zh_message(err)}，收到 {_fmt_value(err.get('input'))}")
    if not parts:  # pragma: no cover - pydantic 一定给至少一个 error
        return "参数不合法"
    return "；".join(parts)


def validate_tool_args(model: type[BaseModel], args: dict | None) -> str | None:
    """校验工具入参；合法返回 None，否则返回结构化 INVALID_ARGS 错误 JSON。"""
    try:
        model.model_validate(args or {})
        return None
    except ValidationError as exc:
        return tool_error(
            ErrorCode.INVALID_ARGS,
            format_validation_error(exc),
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001
        # 模型本身写错等意外：不让它把工具调用链炸掉
        return tool_error(ErrorCode.INVALID_ARGS, f"参数校验失败：{exc}", retryable=True)


# ── 高频工具入参模型（先覆盖 6 个）───────────────────────────────────────
class _ToolArgs(BaseModel):
    """所有工具入参模型的基类：多余键忽略（执行器会注入 canvas_id 等）。"""

    model_config = ConfigDict(extra="ignore")


class QuerySqlArgs(_ToolArgs):
    """query_sql：数据源 + 只读 SQL。

    datasource_id 不强制必填：schema 里它是 required，但本仓存在"只带 sql"的历史
    调用路径（含既有单测的假工具）；缺 datasource_id 时留给工具自身报错，校验只负责
    类型 / 非空 SQL（这才是真正会在执行层炸掉的部分）。
    """

    datasource_id: str = ""
    sql: str = Field(min_length=1)


class QueryEngineArgs(_ToolArgs):
    """query_engine：结构化查询配置（维度/度量/过滤/排序/时间桶）。"""

    datasource_id: str = Field(min_length=1)
    dimensions: list = Field(default_factory=list)
    measures: list = Field(default_factory=list)
    filters: list = Field(default_factory=list)
    sort: dict | None = None
    limit: int = Field(default=50, ge=1, le=10000)
    dimension_buckets: dict[str, str] | None = None


class ListFieldsArgs(_ToolArgs):
    """list_fields：单个数据源。"""

    datasource_id: str = Field(min_length=1)


class RenderChartArgs(_ToolArgs):
    """render_chart：四个参数缺一不可，但缺参由工具给出更丰富的引导（含图表类型表）。

    这里只校验类型/结构，不强制必填，避免把工具的友好报错盖成通用 INVALID_ARGS。
    """

    chart_type: str = ""
    title: str = ""
    columns: list = Field(default_factory=list)
    rows: list = Field(default_factory=list)


class AddChartBlockArgs(_ToolArgs):
    """add_chart_block（画布）：同 render_chart，缺参引导保留在工具实现里。"""

    title: str = ""
    chart_type: str = ""
    datasource_id: str = ""
    dimensions: list = Field(default_factory=list)
    measures: list = Field(default_factory=list)


class QuerySampleArgs(_ToolArgs):
    """query_sample（Lead 观察）：数据源可选（缺省用默认源）。"""

    datasource_id: str | None = None


# 工具名 -> 入参模型（ToolExecutor 调用前解析；add_chart_block/query_sample 不在
# 本仓 agent_tools.py 里，靠这张表覆盖，避免跨模块改动）
ARGS_MODELS: dict[str, type[BaseModel]] = {
    "query_sql": QuerySqlArgs,
    "query_engine": QueryEngineArgs,
    "list_fields": ListFieldsArgs,
    "render_chart": RenderChartArgs,
    "add_chart_block": AddChartBlockArgs,
    "query_sample": QuerySampleArgs,
}


def resolve_args_model(tool: Any, name: str) -> type[BaseModel] | None:
    """解析工具的入参模型：优先 BaseTool.args_model，其次按工具名查表。

    命中查表时顺手挂到工具实例上（自描述：之后读 tool.args_model 也能看到）。
    """
    model = getattr(tool, "args_model", None)
    if model is not None:
        return model
    model = ARGS_MODELS.get(name)
    if model is not None:
        try:
            tool.args_model = model
        except Exception:  # noqa: BLE001
            pass
    return model
