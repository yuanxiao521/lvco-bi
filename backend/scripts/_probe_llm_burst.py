# -*- coding: utf-8 -*-
"""量化上游 LLM 的当前稳定性：连续 6 次调用，看失败率与错误类型。"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
from app.services.llm_client import LLMClient

async def main():
    c = LLMClient()
    ok = 0
    for i in range(1, 7):
        try:
            out = await c.complete([{"role": "user", "content": f"回复数字 {i}"}], max_tokens=256)
            ok += 1
            print(f"  {i}: OK   {out[:20]!r}")
        except Exception as e:
            print(f"  {i}: FAIL {type(e).__name__}: {str(e)[:150]}")
        await asyncio.sleep(1)
    print(f"\n成功率 {ok}/6")

asyncio.run(main())
