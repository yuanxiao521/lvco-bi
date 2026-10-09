"""D4.4 记忆 · 连续多轮压缩，观察【压缩摘要】链式累积。

目标：揭示一个真实风险——多轮压缩会让摘要链越积越长，
这也是升级设计里 `LeadContext.digest(max_chars=4000)` 的动机。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_marker_chain.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.context_utils import (  # noqa: E402
    COMPRESSION_MARKER,
    count_rounds_since_marker,
    extract_compressed_digest,
    smart_compress_history,
)


class MockSummarizer:
    def __init__(self, width: int = 120) -> None:
        self.width = width

    async def complete(self, messages, **kw) -> str:  # noqa: ANN001
        raw = " ".join(str(m.get("content", "")) for m in messages)
        return raw[: self.width]


def append_rounds(messages: list[dict], start: int, count: int) -> None:
    for i in range(start, start + count):
        messages += [
            {"role": "user", "content": f"第 {i} 轮：再看下 {i} 月数据"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"c{i}", "type": "function",
                    "function": {"name": "query_sql", "arguments": json.dumps({"sql": f"select {i}"})},
                }],
            },
            {"role": "tool", "tool_call_id": f"c{i}", "content": json.dumps({"rows": [[1, 2]] * 3})},
            {"role": "assistant", "content": f"第 {i} 轮结论：{i * 10}。"},
        ]


async def main() -> None:
    llm = MockSummarizer(width=120)
    messages: list[dict] = [{"role": "system", "content": "你是数据助手。"}]
    append_rounds(messages, 1, 4)

    for rnd in range(1, 4):
        messages = await smart_compress_history(messages, llm, min_rounds=1, keep_rounds=2)
        digest = extract_compressed_digest(messages)
        markers = sum(1 for m in messages if str(m.get("content", "")).startswith(COMPRESSION_MARKER))
        print(f"第 {rnd} 次压缩后：消息 {len(messages)} 条，摘要标记 {markers} 个，"
              f"digest 长度 {len(digest)}，距上次标记轮次 {count_rounds_since_marker(messages)}")
        append_rounds(messages, 10 * rnd, 2)

    print()
    print("最终 digest（截断展示）：")
    print("  " + extract_compressed_digest(messages)[:240].replace("\n", "\n  "))
    print()
    print("观察点：")
    print("  - 每轮压缩新增一个【压缩摘要】标记，digest 随之变长")
    print("  - 无上限时会持续膨胀 → 需要 LeadContext.digest(max_chars=4000) 兜底")


if __name__ == "__main__":
    asyncio.run(main())
