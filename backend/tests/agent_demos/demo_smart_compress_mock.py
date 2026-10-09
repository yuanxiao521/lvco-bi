"""D4.3 记忆 · smart_compress_history（mock LLM 摘要）。

目标：演示"窗口保留 + LLM 摘要"：保留最后 keep_rounds 轮完整上下文，
更早轮次的 tool 结果被替换为摘要，但 **tool_call 消息保留不动**（参数供归因/重试）。

mock LLM 只做"截断成 200 字"，重点是验证结构而非摘要质量。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_smart_compress_mock.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.context_utils import (  # noqa: E402
    COMPRESSION_MARKER,
    smart_compress_history,
)


class MockSummarizer:
    """最小 LLM 契约：把输入截断成 200 字当作摘要。"""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        self.calls += 1
        raw = " ".join(str(m.get("content", "")) for m in messages)
        return f"摘要：{raw[:200]}"


def build_messages(rounds: int = 6) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "你是 LvcoBI 数据助手。"}]
    for i in range(1, rounds + 1):
        messages += [
            {"role": "user", "content": f"第 {i} 轮：帮我看下 {i} 月销售额"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {"name": "query_sql", "arguments": json.dumps({"sql": f"select * from t where m={i}"})},
                }],
            },
            {"role": "tool", "tool_call_id": f"call_{i}", "content": json.dumps({"rows": [[j, j * 100] for j in range(5)]})},
            {"role": "assistant", "content": f"第 {i} 轮结论：销售额 {i * 100} 万。"},
        ]
    return messages


def stats(messages: list[dict]) -> tuple[int, int, int]:
    tool_calls = sum(1 for m in messages if m.get("tool_calls"))
    tool_msgs = sum(1 for m in messages if m.get("role") == "tool")
    markers = sum(1 for m in messages if str(m.get("content", "")).startswith(COMPRESSION_MARKER))
    return tool_calls, tool_msgs, markers


async def main() -> None:
    messages = build_messages(6)
    llm = MockSummarizer()
    tc0, tool0, mk0 = stats(messages)
    print(f"压缩前：消息 {len(messages)} 条，tool_call {tc0}，tool 结果 {tool0}，摘要标记 {mk0}")

    out = await smart_compress_history(messages, llm, min_rounds=3, keep_rounds=3)
    tc1, tool1, mk1 = stats(out)
    print(f"压缩后：消息 {len(out)} 条，tool_call {tc1}，tool 结果 {tool1}，摘要标记 {mk1}")
    print(f"LLM 调用次数={llm.calls}")
    print()

    print("压缩后消息角色序列：")
    for m in out:
        role = m.get("role")
        if m.get("tool_calls"):
            role += "(tool_calls)"
        content = str(m.get("content", ""))[:36]
        print(f"  {role:<18} {content}")
    print()
    print("观察点：")
    print("  - tool_call 消息被保留（参数细节在）→ tool 结果被摘要替换")
    print("  - 最后 keep_rounds 轮完整保留（不压缩）")
    print("  - 摘要行以【压缩摘要】开头，供 extract_compressed_digest 回收")


if __name__ == "__main__":
    asyncio.run(main())
