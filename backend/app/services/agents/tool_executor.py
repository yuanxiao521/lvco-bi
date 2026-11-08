"""共享工具执行内核：统一「解析参数 → 查注册 → 执行 → 观测 → 判定 → emit」契约。

背景：orchestrator 的 mini-ReAct 与 react_agent 的 ReAct 循环此前各自复制了一份
工具执行逻辑（参数解析、注册查找、观测 span、异常兜底、错误判定、事件 emit、
render_chart 事件转发），改一处工具调用契约要同步改两处，迟早漏改。

本模块把「单次工具调用的完整生命周期」抽成 ToolExecutor + 一组纯函数，
两个 Agent 只保留各自的编排策略（并行/串行、memo、失败签名、熔断、phase 流转）。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import settings
from app.services.agent_tools import ToolRegistry
from app.services.agents.tool_contract import (
    ErrorCode,
    error_kind_for,
    resolve_args_model,
    tool_error,
    validate_tool_args,
)
from app.services.observability import observe_tool_call

logger = logging.getLogger(__name__)


@dataclass
class ToolCallResult:
    """单次工具调用的结构化执行结果，供上层编排策略消费。"""

    name: str                               # 工具名
    args: dict                              # 实际使用的参数（含 orchestrator 填充后的数据）
    result: str                             # 工具返回的原始 JSON 字符串
    is_error: bool                          # 结果是否含 error
    fatal: bool = False                     # 未知工具等不可恢复错误
    memo_hit: bool = False                  # 是否命中 memo 缓存
    tc: dict = field(default_factory=dict)  # 原始 tool_call（用于 tool_call_id）


def parse_tool_arguments(tc: dict) -> dict:
    """解析工具调用的 arguments JSON；解析失败返回空 dict。"""
    try:
        return json.loads(tc.get("arguments", "{}") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def is_error_result(result_str: str) -> bool:
    """判断工具返回的 JSON 字符串是否为错误结果（含 error 键）。"""
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def build_assistant_message(tool_calls: list[dict], reasoning_content: str = "", id_offset: int = 0) -> dict:
    """构建包含 tool_calls 的 assistant 消息（OpenAI function calling 格式）。

    Args:
        tool_calls: LLM 返回的原始 tool_call 列表。
        reasoning_content: DeepSeek reasoning 模式需要回传的推理内容。
        id_offset: 兜底 tool_call_id 的起始序号（LLM 未返回 id 时用于对齐 tool 结果）。
    """
    assistant_tool_calls = []
    for i, tc in enumerate(tool_calls):
        assistant_tool_calls.append({
            "id": tc.get("id", f"call_{id_offset + i}"),
            "type": "function",
            "function": {
                "name": tc.get("name", ""),
                "arguments": tc.get("arguments", "{}"),
            },
        })
    msg: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": assistant_tool_calls}
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    return msg


def _make_memo_key(tool_name: str, args: dict) -> str:
    """生成幂等 memo key：`tool_name:sha1(args)[:8]`（与 orchestrator 失败签名同构）。"""
    h = hashlib.sha1(json.dumps(args or {}, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{tool_name}:{h[:8]}"


# ── P1-C：agent_calls 采集辅助（脱敏在 agent_trace 落库前统一做）────────────
def _args_hash(args: dict) -> str:
    """与 memo key 同构的 8 位参数指纹（同名工具同参数的调用可聚合）。"""
    try:
        raw = json.dumps(args or {}, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return ""


def _classify_error(msg: str) -> str:
    """把工具错误的自由文本归到有限枚举（供 error_kind 聚合统计）。"""
    text = str(msg or "")
    low = text.lower()
    if "敏感" in text or "sensitive" in low:
        return "sensitive_column"
    if "超时" in text or "timeout" in low:
        return "query_timeout"
    if "不在当前入口允许范围" in text:
        return "whitelist"
    if "未知工具" in text:
        return "unknown_tool"
    return "tool_error"


def _extract_blocked_columns(msg: str) -> list[str]:
    """从"…：col1、col2"式错误文案里抽出被拦的列名（排障线索，不整体 omit）。"""
    try:
        tail = str(msg).replace(":", "：").split("：")[-1]
        parts = [part.strip(" \t\"'") for part in re.split(r"[、,，]", tail)]
        return [part for part in parts if part][:20]
    except Exception:  # noqa: BLE001
        return []


# P3-A：瞬时错误不该进 memo（否则一次超时会被缓存成"永远超时"）
_TRANSIENT_CODES = frozenset({"TOOL_TIMEOUT", "TOOL_EXCEPTION", "UPSTREAM"})


def _is_transient_result(result_str: str) -> bool:
    try:
        parsed = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return str(parsed.get("code") or "") in _TRANSIENT_CODES


async def _noop_emit(_ev: dict) -> None:
    pass


class ToolExecutor:
    """共享工具执行器：负责单次工具调用的完整生命周期。

    上层 Agent 循环负责决策（调哪些工具、失败如何重试），本执行器负责
    「安全地把一个工具调用跑完」——幂等 memo、观测 span、异常兜底、
    事件 emit 与 render_chart 事件转发。

    用法：
        executor = ToolExecutor(user_id=uid, db_session=db, emit=emit, trace=trace)
        result = await executor.execute_tool_call(tc, args=prefilled_args)
    """

    def __init__(
        self,
        *,
        user_id: str,
        db_session,
        emit: Callable[[dict], Awaitable[None]] | None = None,
        trace=None,
        memo: dict | None = None,
        memo_locks: dict | None = None,
        idempotent_tools: frozenset[str] = frozenset(),
        success_cached_tools: frozenset[str] = frozenset(),
        allowed_tools: set[str] | None = None,
        context: dict | None = None,
    ):
        self.user_id = user_id
        self.db_session = db_session
        self.emit = emit or _noop_emit
        self.trace = trace
        self.memo = memo
        self.memo_locks = memo_locks
        self.idempotent_tools = idempotent_tools
        # 成功态缓存：结果成功才写 memo（error 不缓存，避免固化错误阻断 LLM 自纠错）
        self.success_cached_tools = success_cached_tools
        # 入口工具白名单：非 None 时，白名单外的工具调用一律拒绝（受限入口防越权）。
        # None 表示不校验（普通 chat / react 路径保持原行为）。
        self.allowed_tools = allowed_tools
        # 执行器级上下文（如画布入口的 canvas_id）：由系统绑定，缺省参数时自动补进工具
        self.context = {k: v for k, v in (context or {}).items() if v}

    def _inject_context(self, tool, targs: dict) -> dict:
        """把执行器级上下文（如 canvas_id）补进本次工具参数。

        画布入口的 canvas_id 由系统绑定（前端请求 → Lead ctx → 执行器），
        不该让 LLM 抄写或猜测：它看不见 id 时只会留空导致工具报"缺少画布上下文"。
        LLM 显式传入的同名参数优先；仅当工具签名能接收该参数时才注入，
        避免给签名固定的工具塞参数抛 TypeError。
        """
        if not self.context:
            return targs
        try:
            params = inspect.signature(tool.execute).parameters
        except (TypeError, ValueError):
            return targs
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        extra = {
            k: v for k, v in self.context.items()
            if k not in targs and (has_var_kw or k in params)
        }
        return {**targs, **extra} if extra else targs

    async def execute_tool_call(self, tc: dict, args: dict | None = None) -> ToolCallResult:
        """解析 + 执行单个工具调用，返回结构化结果。

        - 未知工具 → fatal=True（不可恢复）
        - 幂等工具且命中 memo → 返回缓存结果（memo_hit=True）
        - 其余 → 执行工具（观测 span + 异常兜底）并判定 is_error
        - 统一 emit tool_call / tool_result / chart 事件
        """
        tname = tc.get("name", "")
        targs = args if args is not None else parse_tool_arguments(tc)
        started = time.perf_counter()  # P1-C：duration_ms（工具本身可能很慢，必须计时）
        await self.emit({"type": "tool_call", "name": tname, "args": targs})

        # 入口工具白名单校验：受限入口（如画布助手）只允许白名单内工具，
        # 防止 LLM 越权调用当前入口无接收方的工具（如画布下调 render_chart）。
        # 返回 error 结果让 LLM 自纠错（非 fatal，不中断整个步骤）。
        if self.allowed_tools is not None and tname not in self.allowed_tools:
            # P3-A：结构化错误（仍保留 "error" 键，向后兼容）
            result = tool_error(
                ErrorCode.WHITELIST,
                f"工具 '{tname}' 不在当前入口允许范围内，请改用允许的工具",
                retryable=False,
            )
            await self.emit({"type": "tool_result", "name": tname, "result": result})
            self._record_agent_call(
                tc, targs, tname, result, ok=False,
                error_kind=error_kind_for(ErrorCode.WHITELIST), started=started,
            )
            return ToolCallResult(name=tname, args=targs, result=result, is_error=True, fatal=False, tc=tc)

        tool = ToolRegistry.get(tname)
        if tool is None:
            result = tool_error(
                ErrorCode.UNKNOWN_TOOL, f"未知工具: {tname}", retryable=False
            )
            self._record_agent_call(
                tc, targs, tname, result, ok=False,
                error_kind=error_kind_for(ErrorCode.UNKNOWN_TOOL), started=started,
            )
            return ToolCallResult(name=tname, args=targs, result=result, is_error=True, fatal=True, tc=tc)

        # P3-A：入参校验（高频工具）。失败**不执行工具**，直接回 INVALID_ARGS +
        # 字段路径，让模型按 hint 修正后重试；避免"跑了一半才炸"或静默 TypeError。
        args_model = resolve_args_model(tool, tname)
        if args_model is not None:
            invalid_args = validate_tool_args(args_model, targs)
            if invalid_args is not None:
                await self.emit({"type": "tool_result", "name": tname, "result": invalid_args})
                self._record_agent_call(
                    tc, targs, tname, invalid_args, ok=False,
                    error_kind=error_kind_for(ErrorCode.INVALID_ARGS), started=started,
                )
                logger.info("[tool_executor] invalid_args tool=%s", tname)
                return ToolCallResult(
                    name=tname, args=targs, result=invalid_args,
                    is_error=True, fatal=False, tc=tc,
                )

        if tname in self.idempotent_tools and self.memo is not None:
            result, memo_hit = await self._exec_with_memo(tname, targs, tool, cache_errors=True)
        elif tname in self.success_cached_tools and self.memo is not None:
            # 成功态缓存：error 不写入 memo（避免固化错误阻断 LLM 自纠错）
            result, memo_hit = await self._exec_with_memo(tname, targs, tool, cache_errors=False)
        else:
            result, memo_hit = await self._execute_once(tool, tname, targs), False

        is_error = is_error_result(result)
        await self.emit({"type": "tool_result", "name": tname, "result": result, "memo": memo_hit})
        self._record_agent_call(
            tc, targs, tname, result,
            ok=not is_error, started=started, memo_hit=memo_hit,
        )

        # render_chart 成功时转发 chart 事件（供前端直接渲染）
        if not is_error and tname == "render_chart":
            try:
                cr = json.loads(result)
                if cr.get("option"):
                    await self.emit({"type": "chart", "chart_type": cr.get("chart_type", "bar"), "option": cr["option"]})
            except Exception as ce:
                logger.warning("chart 事件解析失败: %s", ce)

        return ToolCallResult(
            name=tname, args=targs, result=result,
            is_error=is_error, fatal=False, memo_hit=memo_hit, tc=tc,
        )

    def _record_agent_call(
        self,
        tc: dict,
        targs: dict,
        tname: str,
        result,
        *,
        ok: bool,
        error_kind: str | None = None,
        started: float | None = None,
        memo_hit: bool = False,
    ) -> None:
        """P1-C：把一次工具调用写入 agent_calls（同步、绝不抛；未绑定 trace 自动跳过）。

        白名单/未知工具两条"静默拒绝"分支也在这里留痕 —— 越权尝试是最该被审计的一类事件。
        """
        try:
            from app.services import agent_trace

            if not agent_trace.enabled():
                return
            parsed = None
            try:
                parsed = json.loads(result) if isinstance(result, str) else None
            except (json.JSONDecodeError, TypeError):
                parsed = None
            err_msg = None
            err_layer = None
            blocked: list[str] = []
            if isinstance(parsed, dict):
                # P4-B：guard 的分层（L1-L3；AST 折进 L3 时工具会标 4）
                try:
                    raw_layer = parsed.get("layer")
                    err_layer = int(raw_layer) if raw_layer is not None else None
                except (TypeError, ValueError):
                    err_layer = None
            if isinstance(parsed, dict) and parsed.get("error"):
                err_msg = str(parsed.get("error"))
                if not error_kind:
                    # P3-A：结构化错误的 code 优先（与 tool_contract 的映射对齐）
                    error_kind = error_kind_for(parsed.get("code"))
                if not error_kind:
                    error_kind = _classify_error(err_msg)
                if error_kind == "sensitive_column":
                    blocked = _extract_blocked_columns(err_msg)
            agent_trace.record({
                "kind": "tool",
                "tool_name": tname,
                "tool_call_id": str(tc.get("id") or "") or None,
                "args": targs,
                "args_hash": _args_hash(targs),
                "ok": bool(ok),
                "error_kind": error_kind,
                "error_layer": err_layer,
                "error_msg": err_msg,
                "result_chars": len(result) if isinstance(result, str) else None,
                "result_preview": result[:500] if isinstance(result, str) else None,
                "duration_ms": int((time.perf_counter() - started) * 1000) if started else None,
                "attempt": 1,
                "memo_hit": bool(memo_hit),
                "blocked_columns": blocked,
            })
        except Exception:  # noqa: BLE001
            logger.debug("agent_trace tool record failed", exc_info=True)

    async def _exec_with_memo(self, tname: str, targs: dict, tool, cache_errors: bool = True) -> tuple[str, bool]:
        """幂等/可缓存工具执行：命中 memo 直接返回缓存，否则执行并写缓存（按 key 加锁防并发重复）。

        Args:
            cache_errors: True（幂等工具）→ 无条件写缓存；
                          False（成功态工具如 query_sql）→ 仅结果成功才写缓存，
                          失败/错误结果不缓存，保证 LLM 自纠错不被旧错误锚定。
        """
        mkey = _make_memo_key(tname, targs)
        lock = None
        if self.memo_locks is not None:
            lock = self.memo_locks.setdefault(mkey, asyncio.Lock())

        async def _run() -> tuple[str, bool]:
            if mkey in self.memo:
                return self.memo[mkey], True
            result = await self._execute_once(tool, tname, targs)
            if (cache_errors or not is_error_result(result)) and not _is_transient_result(result):
                self.memo[mkey] = result
            return result, False

        if lock is not None:
            async with lock:
                return await _run()
        return await _run()

    async def _execute_once(self, tool, tname: str, targs: dict) -> str:
        """执行单次工具调用（观测 span + 统一超时 + 异常兜底），返回结果字符串。

        P3-A：最外层统一超时（settings.TOOL_TIMEOUT_SECONDS）。工具内部可能还有
        自己的超时（如 SQL 查询超时），双层并存时**外层兜底**：内层先触发会返回
        它自己的错误；内层没触发（挂死/慢工具）由外层 wait_for 取消并返回 TOOL_TIMEOUT。
        """
        targs = self._inject_context(tool, targs)
        span_obj = None
        timeout = float(getattr(settings, "TOOL_TIMEOUT_SECONDS", 60.0) or 0)

        async def _call() -> str:
            return await tool.execute(
                user_id=self.user_id, db_session=self.db_session, **targs
            )

        async def _run() -> str:
            if timeout > 0:
                return await asyncio.wait_for(_call(), timeout=timeout)
            return await _call()

        try:
            if self.trace is not None:
                with observe_tool_call(self.trace, tname, args=targs) as span:
                    span_obj = span
                    result_str = await _run()
            else:
                result_str = await _run()
        except asyncio.TimeoutError:
            logger.warning("[tool_executor] tool_timeout tool=%s timeout=%ss", tname, timeout)
            result_str = tool_error(
                ErrorCode.TIMEOUT,
                "工具执行超时",
                retryable=True,
                hint="缩小查询范围或加筛选条件后重试",
            )
        except Exception as e:
            result_str = tool_error(ErrorCode.TOOL_EXCEPTION, str(e))
        if span_obj is not None:
            try:
                parsed = json.loads(result_str)
                is_error = isinstance(parsed, dict) and "error" in parsed
            except Exception:
                is_error = False
            span_obj.update(output=result_str[:300], metadata={"ok": not is_error, "attempt": 1})
        return result_str
