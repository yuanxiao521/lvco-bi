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
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.services.agent_tools import ToolRegistry
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
    ):
        self.user_id = user_id
        self.db_session = db_session
        self.emit = emit or _noop_emit
        self.trace = trace
        self.memo = memo
        self.memo_locks = memo_locks
        self.idempotent_tools = idempotent_tools
        # 成功态缓存工具：结果成功才写 memo（error 不缓存，避免固化错误阻断自纠错）。
        # 用于非幂等但静态数据下"同参数同结果"的工具（如 query_sql 任务内复用）。
        self.success_cached_tools = success_cached_tools
        # 入口工具白名单：非 None 时，白名单外的工具调用一律拒绝（受限入口防越权）。
        # None 表示不校验（普通 chat / react 路径保持原行为）。
        self.allowed_tools = allowed_tools

    async def execute_tool_call(self, tc: dict, args: dict | None = None) -> ToolCallResult:
        """解析 + 执行单个工具调用，返回结构化结果。

        - 未知工具 → fatal=True（不可恢复）
        - 幂等工具且命中 memo → 返回缓存结果（memo_hit=True）
        - 其余 → 执行工具（观测 span + 异常兜底）并判定 is_error
        - 统一 emit tool_call / tool_result / chart 事件
        """
        tname = tc.get("name", "")
        targs = args if args is not None else parse_tool_arguments(tc)
        await self.emit({"type": "tool_call", "name": tname, "args": targs})

        # 入口工具白名单校验：受限入口（如画布助手）只允许白名单内工具，
        # 防止 LLM 越权调用当前入口无接收方的工具（如画布下调 render_chart）。
        # 返回 error 结果让 LLM 自纠错（非 fatal，不中断整个步骤）。
        if self.allowed_tools is not None and tname not in self.allowed_tools:
            result = json.dumps(
                {"error": f"工具 '{tname}' 不在当前入口允许范围内，请改用允许的工具"},
                ensure_ascii=False,
            )
            await self.emit({"type": "tool_result", "name": tname, "result": result})
            return ToolCallResult(name=tname, args=targs, result=result, is_error=True, fatal=False, tc=tc)

        tool = ToolRegistry.get(tname)
        if tool is None:
            result = json.dumps({"error": f"未知工具: {tname}"}, ensure_ascii=False)
            return ToolCallResult(name=tname, args=targs, result=result, is_error=True, fatal=True, tc=tc)

        if tname in self.idempotent_tools and self.memo is not None:
            result, memo_hit = await self._exec_with_memo(tname, targs, tool, cache_errors=True)
        elif tname in self.success_cached_tools and self.memo is not None:
            # 成功态缓存：error 不写入 memo（避免固化错误阻断 LLM 自纠错）
            result, memo_hit = await self._exec_with_memo(tname, targs, tool, cache_errors=False)
        else:
            result, memo_hit = await self._execute_once(tool, tname, targs), False

        is_error = is_error_result(result)
        await self.emit({"type": "tool_result", "name": tname, "result": result, "memo": memo_hit})

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
            if cache_errors or not is_error_result(result):
                self.memo[mkey] = result
            return result, False

        if lock is not None:
            async with lock:
                return await _run()
        return await _run()

    async def _execute_once(self, tool, tname: str, targs: dict) -> str:
        """执行单次工具调用（观测 span + 异常兜底），返回结果字符串。"""
        span_obj = None
        try:
            if self.trace is not None:
                with observe_tool_call(self.trace, tname, args=targs) as span:
                    span_obj = span
                    result_str = await tool.execute(user_id=self.user_id, db_session=self.db_session, **targs)
            else:
                result_str = await tool.execute(user_id=self.user_id, db_session=self.db_session, **targs)
        except Exception as e:
            result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
        if span_obj is not None:
            try:
                parsed = json.loads(result_str)
                is_error = isinstance(parsed, dict) and "error" in parsed
            except Exception:
                is_error = False
            span_obj.update(output=result_str[:300], metadata={"ok": not is_error, "attempt": 1})
        return result_str
