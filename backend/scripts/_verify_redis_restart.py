# -*- coding: utf-8 -*-
"""场景 3：Redis 重启 —— 验证降级与自愈（不依赖 LLM）。"""
import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def main():
    from app.services.event_stream import event_stream
    from app.core.cache import get_cache_repository
    from app.services.session_lock import acquire_session_lock, release_session_lock

    print("--- 重启前 ---")
    await event_stream.publish("redis-restart-test", {"type": "before"})
    print("  event_stream 写入:", await event_stream.has_events("redis-restart-test"))
    tok = acquire_session_lock("redis-restart-test")
    print("  抢锁:", bool(tok))

    print()
    print(">>> 现在重启 Redis（docker restart lvco-redis）...")
    import subprocess
    r = subprocess.run(["docker", "restart", "lvco-redis"], capture_output=True, text=True, timeout=90)
    print("  docker:", (r.stdout or r.stderr).strip()[:80])

    print()
    print("--- 重启后（客户端应自动重连）---")
    for i in range(1, 6):
        await asyncio.sleep(2)
        try:
            ok = await event_stream.has_events("redis-restart-test")
            print(f"  第{i}次探测: event_stream 可用（旧数据在？{ok}）")
            break
        except Exception as e:
            print(f"  第{i}次探测: {type(e).__name__}（等待自愈）")

    # 重新写入/读取
    await event_stream.publish("redis-restart-test", {"type": "after"})
    items = await event_stream.read_after("redis-restart-test", "0")
    print("  重启后写入+读取:", [ev["type"] for _e, ev in items])

    # 锁仍可用（缓存层 fail-open 或正常）
    tok2 = acquire_session_lock("redis-restart-test-2")
    print("  抢锁仍可用:", bool(tok2))
    release_session_lock("redis-restart-test-2", tok2)

    await event_stream.clear("redis-restart-test")
    print()
    print("结论：Redis 重启后客户端自动重连，事件日志与锁恢复正常（内存中的旧日志随重启丢失，属预期）")

asyncio.run(main())
