"""D5.1 监控 · 不启用 Langfuse，Observer 在本地模式下如何输出日志。

目标：跑一次完整 trace（含 2 个 span），观察
1) `trace_local` 总行格式（name / latency_ms / children 数）；
2) `trace_span` 逐 span 行格式（type/name/latency_ms/input/output/error）；
3) `latency_ms` 如何由 start/end 时间戳计算；
4) LLM 调用 / 工具调用各有专属包装（observe_llm_call / observe_tool_call）。

关键契约（observability.py）：
- `settings.is_langfuse_configured=False` → `_langfuse_client=None` → 本地模式
- `Observer.trace(...)` 是 contextmanager：with 块结束自动 `finish()` 并打日志
- `span.update(output=...)` 在退出上下文前回填，日志里就能看到摘要
- 输入/输出经 `_summarize` 截断（默认 500 字符，输出取尾部更易看到报错）

运行（在 backend 目录下）：
    python tests/agent_demos/demo_observer_local.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.services.observability import (  # noqa: E402
    get_observer,
    observe_llm_call,
    observe_tool_call,
)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(levelname)s | %(message)s")

    observer = get_observer()
    print(f"LANGFUSE_ENABLED = {settings.LANGFUSE_ENABLED}")
    print(f"observer.enabled = {observer.enabled}  ->  {'启用 Langfuse' if observer.enabled else '未配置，走本地日志'}\n")

    with observer.trace(
        "agent_session_demo",
        user_id="user_demo_001",
        session_id="sess_demo_001",
        metadata={"task": "D5.1 observer 本地日志演示"},
    ) as trace:
        time.sleep(0.01)
        # ① LLM 调用：span_type = "generation"
        with observe_llm_call(
            trace,
            "intent_classify",
            messages=[{"role": "user", "content": "分析本月各区域销量"}],
            model="gpt-4o-mini",
        ) as llm_span:
            time.sleep(0.02)
            llm_span.update(output='{"intent": "analysis", "confidence": 0.9}')

        # ② 工具调用：span_type = "tool"（带 SQL 参数，日志里会看到摘要）
        with observe_tool_call(
            trace,
            "query_sql",
            args={"sql": "SELECT region, SUM(amount) AS sales FROM orders GROUP BY region"},
        ) as tool_span:
            time.sleep(0.03)
            tool_span.update(output='{"error": "字段 region 不存在", "hint": "请用 area"}')

    await asyncio.sleep(0.1)

    print("\n┌─ 观察点 ─────────────────────────────────────────────")
    print("│ 1. 日志第一行 trace_local：整个 trace 的总耗时与 span 数     ")
    print("│ 2. 日志随后两行 trace_span：每个 span 的耗时与输入输出摘要    ")
    print("│ 3. 工具 span 的 output 含 error：日志仍正常输出（错误也记录）")
    print("│ 4. llm span 的 name 带 llm: 前缀，tool span 带 tool: 前缀    ")
    print("│ 5. latency_ms = (end_time - start_time) * 1000（毫秒整数）   ")
    print("└───────────────────────────────────────────────────────")


if __name__ == "__main__":
    asyncio.run(main())