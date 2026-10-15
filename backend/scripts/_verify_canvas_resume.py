# -*- coding: utf-8 -*-
"""真机验证：画布入口的断线续收（画布改造 B 的验收）。

改造前：画布 resume 只看**进程内注册表**（chat_registry.get），
        且重连只能补发一段"文本快照"——decision / tool_call / progress
        这类非文本事件在断线瞬间永久丢失。
改造后：事件已落 Redis Stream，resume 从游标 0 回放 + 续读，所有事件类型都在。

验证手法（模拟真实"刷新页面"）：
  1. 发起画布对话，只消费前几个事件就**主动断开连接**（任务后台继续跑）
  2. 立刻用 resume=true + 同一 session_id 重连
  3. 断言：
     a) 重连流里出现了**非文本事件**（decision / tool_call / progress）
        —— 这是旧实现无论如何拿不到的（它只能补文本）
     b) 重连流以 done 收尾
     c) 重连期间 /canvases/{cid}/sessions 的 running=true（前端自动续收的触发依据）

用法：$env:PYTHONIOENCODING='utf-8'; .venv\\Scripts\\python.exe scripts/_verify_canvas_resume.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402
import redis  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.models.user import User  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
DS = "970d798d-f5da-4442-8d25-3563020d5a29"

# 非文本事件：改造前不可能通过续收拿到的那些
NON_TEXT = {"decision", "tool_call", "tool_result", "progress", "intent", "status"}


async def main() -> int:
    async with async_session_factory() as db:
        u = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one()
    h = {"Authorization": f"Bearer {create_access_token(str(u.id))}"}
    sse_h = {**h, "Accept": "text/event-stream"}
    rc = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as c:
        r = await c.post(f"{BASE}/canvases", headers=h,
                         json={"title": "画布续收验证", "datasource_id": DS}, timeout=30)
        cid = r.json()["data"]["id"]
        print(f"画布 = {cid[:8]}")

        payload = {"datasource_id": DS, "session_id": None,
                   "message": "查询各地区销售额，做一个柱状图并给出简短分析",
                   "canvas_id": cid, "new_session": True}

        # ── 1. 发起任务，只读前几个事件就断开（模拟刷新）──────────────────────
        sid = None
        first_types: list[str] = []
        t0 = time.time()
        async with c.stream("POST", f"{BASE}/ai/canvas/chat", json=payload,
                            headers=sse_h, timeout=300) as resp:
            assert resp.status_code == 200, resp.text[:200]
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                try:
                    ev = json.loads(raw[6:].strip())
                except Exception:
                    continue
                t = ev.get("type")
                first_types.append(t)
                if t == "session_created":
                    sid = ev.get("session_id") or (ev.get("session") or {}).get("id")
                # 拿到 session_id 且已收到若干事件 → 主动断开
                if sid and len(first_types) >= 6:
                    break
        assert sid, f"未拿到 session_id（首连事件: {first_types}）"
        print(f"会话 = {sid[:8]}")
        print(f"首连收到 {len(first_types)} 个事件后断开: {first_types}")

        # 断开后确认任务仍在后台跑 → running=true（前端据此自动续收）
        running_during = None
        for _ in range(20):
            rr = await c.get(f"{BASE}/ai/canvases/{cid}/sessions", headers=h, timeout=20)
            for it in rr.json()["data"]:
                if it["id"] == sid:
                    running_during = it.get("running")
            if running_during:
                break
            await asyncio.sleep(0.5)
        print(f"断开后 /canvases/.../sessions running = {running_during}")

        # ── 2. resume 重连（模拟刷新后自动续收）──────────────────────────────
        replay_types: list[str] = []
        text = ""
        saw_done = False
        async with c.stream("POST", f"{BASE}/ai/canvas/chat",
                            json={"resume": True, "session_id": sid,
                                  "canvas_id": cid, "datasource_id": DS},
                            headers=sse_h, timeout=300) as resp:
            assert resp.status_code == 200, resp.text[:200]
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                try:
                    ev = json.loads(raw[6:].strip())
                except Exception:
                    continue
                t = ev.get("type")
                replay_types.append(t)
                if t == "message":
                    text += ev.get("delta") or ""
                if t == "done":
                    saw_done = True
                    break
        await asyncio.sleep(0.5)

        # ── 3. 基准：直接从 Redis 读**原始事件日志**（不经应用），作为回放完整性的真值 ──
        # 用外部连接读，避免"拿应用的读路径去证明应用写对了"这种自证。
        entries = rc.xrange(f"ai:events:{sid}")
        logged = [json.loads(f["d"]) for _eid, f in entries]
        logged_types = {e.get("type") for e in logged}
        logged_text = "".join(
            str(e.get("delta") or "") for e in logged if e.get("type") == "message"
        )
        # canvas_action 是**非幂等副作用**（重复落块），回放时被刻意跳过——画布真值在 DB
        expect_replay = logged_types - {"canvas_action"}

        non_text_replayed = sorted({t for t in replay_types if t in NON_TEXT})
        print(f"日志基准: {len(logged)} 条事件, 类型={sorted(logged_types)}")
        print(f"续收收到 {len(replay_types)} 个事件: {sorted(set(replay_types))}")
        print(f"其中非文本事件 = {non_text_replayed}")
        print(f"续收文本长度 = {len(text)}  日志文本长度 = {len(logged_text)}  以 done 收尾 = {saw_done}")

        missing = expect_replay - set(replay_types)
        checks = {
            "断开后任务仍在跑（running=true，前端自动续收的依据）": running_during is True,
            "续收回放出非文本事件（旧实现只能补文本，这些拿不到）": bool(non_text_replayed),
            "回放集合 == 日志集合（除刻意跳过的 canvas_action）": not missing,
            "回放文本 == 日志中全部 message 累加（非半截快照）": text == logged_text,
            "续收以 done 收尾（前端据此解锁）": saw_done,
        }
        if missing:
            print(f"  缺失事件类型: {sorted(missing)}")
        print()
        for k, v in checks.items():
            print(f"  [{'OK' if v else 'NG'}] {k}")
        ok = all(checks.values())
        print("\n结论:", "✅ 画布断线续收真机验证通过" if ok else "❌ 未通过")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
