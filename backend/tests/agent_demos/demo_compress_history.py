"""D4.1 记忆 · compress_history 截断兜底。

目标：演示最"粗暴"的记忆压缩——只保留 system + 最近 keep 条，
更早的部分折叠成一条"已省略"提示行（不调用 LLM）。

运行（在 backend 目录下）：
    python tests/agent_demos/demo_compress_history.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.context_utils import compress_history  # noqa: E402


def build_messages(n: int = 100) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "你是 LvcoBI 数据助手。"}]
    for i in range(1, n):
        role = "user" if i % 2 == 1 else "assistant"
        messages.append({"role": role, "content": f"第 {i} 条消息：这是用于演示的对话内容。"})
    return messages


def main() -> None:
    messages = build_messages(100)
    keep = 60
    compressed = compress_history(messages, keep=keep)

    print(f"原始消息数：{len(messages)}")
    print(f"keep={keep}")
    print(f"压缩后消息数：{len(compressed)}")
    print()
    print("压缩后结构：")
    print(f"  [0] {compressed[0]['role']}：{compressed[0]['content'][:40]}")
    if len(compressed) > 1 and "已省略" in str(compressed[1].get("content", "")):
        print(f"  [1] {compressed[1]['role']}（摘要行）：{compressed[1]['content']}")
    print(f"  尾部保留 {keep} 条：首条='{compressed[2]['content'][:20]}' … "
          f"末条='{compressed[-1]['content'][:20]}'")
    print()
    print("观察点：")
    print("  - system 永远保留在头部")
    print("  - 只保留最近 keep 条原文，其余折叠成一行提示")
    print("  - 这是 truncation 兜底，不生成语义摘要（下一步 D4.3 才用 LLM）")


if __name__ == "__main__":
    main()
