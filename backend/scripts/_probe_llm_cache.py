"""探针：确认 DashScope/DeepSeek 的 OpenAI 兼容接口是否返回上下文缓存命中量。

做法：用项目自己的 LLMClient 打两次**相同前缀**的请求（第二次才可能命中缓存），
打印每次返回的 usage meta；重点看有没有 `cached` 字段（前提：llm_client 已解析
prompt_tokens_details.cached_tokens）。不依赖数据库、不写任何文件。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.services.llm_client import LLMClient  # noqa: E402
from app.config import settings  # noqa: E402

# 造一段足够长的公共前缀（缓存通常有最小长度门槛，太短不会命中）
LONG = "你是数据分析助手，严格遵守以下规则：" + "字段名必须与数据源字段完全一致，禁止汉化或臆造；" * 60


async def main() -> None:
    client = LLMClient(settings)
    print(f"model={settings.openai_model} base={settings.openai_base_url}")
    for i in (1, 2):
        messages = [
            {"role": "system", "content": LONG},
            {"role": "user", "content": f"只回答一个词：ok{i}"},
        ]
        try:
            content, meta = await client.complete(messages, max_tokens=16, return_usage=True)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}] 调用失败: {type(e).__name__}: {e}")
            return
        print(f"[{i}] content={content!r} meta={meta}")
        print(f"     → 缓存字段: {meta.get('cached', '未返回（该接口可能不支持或未达最小长度）')}")


if __name__ == "__main__":
    asyncio.run(main())