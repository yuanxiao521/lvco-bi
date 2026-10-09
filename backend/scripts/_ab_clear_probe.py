# -*- coding: utf-8 -*-
"""清空路径 AB 探针：制造画布有块状态（模拟前端 autosave），发清空指令，收集行为证据。

用法：./.venv/Scripts/python.exe scripts/_ab_clear_probe.py --label ON|OFF
观察点：
  - 开关 ON  → 期望日志 clear_canvas_direct + SSE tool_result(clear_canvas) + DB 归零
  - 开关 OFF → 观察 LLM 自选：remove_block 逐个删？add 反而加块？还是正确调 clear_canvas？
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api/v1"
DS_ID = "970d798d-f5da-4442-8d25-3563020d5a29"


def fake_blocks() -> list[dict]:
    return [
        {"id": f"blk-{i}", "type": "text", "title": f"占位文本块{i}", "content": f"测试内容{i}",
         "x": 40, "y": 40 + i * 180, "width": 420, "height": 160}
        for i in range(1, 4)
    ]


async def main_async(args) -> int:
    from app.core.database import async_session_factory
    from app.core.security import create_access_token
    from sqlalchemy import select
    from app.models.user import User

    async with async_session_factory() as db:
        u = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one()
    token = create_access_token(str(u.id))
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=300.0) as c:
        # 1) 建隔离画布
        r = await c.post(f"{BASE}/canvases", headers=headers,
                         json={"title": f"清空AB-{args.label}", "datasourceId": DS_ID})
        r.raise_for_status()
        canvas_id = r.json()["data"]["id"]

        # 2) 模拟前端 autosave：PUT 3 个块（制造"画布有内容"状态）
        r = await c.put(f"{BASE}/canvases/{canvas_id}", headers=headers,
                        json={"blocks": fake_blocks()})
        print(f"[setup] PUT blocks status={r.status_code}")
        r = await c.get(f"{BASE}/canvases/{canvas_id}", headers=headers)
        print(f"[setup] 落块后 DB blocks={len(r.json()['data'].get('blocks') or [])}")

        # 3) 发清空指令，收集 SSE 关键事件
        events = []
        sid = None
        async with c.stream("POST", f"{BASE}/ai/canvas/chat",
                            json={"canvasId": canvas_id, "datasourceId": DS_ID,
                                  "message": "清空所有画布内容"},
                            headers=headers) as resp:
            buf = []
            async for raw in resp.aiter_lines():
                line = raw.rstrip("\r")
                if line.startswith("data:"):
                    buf.append(line[5:].strip())
                elif line == "" and buf:
                    try:
                        ev = json.loads("\n".join(buf))
                    except Exception:
                        buf = []
                        continue
                    buf = []
                    t = ev.get("type", "?")
                    if t in ("decision", "tool_call", "tool_result", "error", "canvas_action"):
                        events.append((t, json.dumps(ev, ensure_ascii=False)[:150]))
                    elif t == "session_created":
                        sid = ev.get("sessionId")

        # 4) 清空后块数
        r = await c.get(f"{BASE}/canvases/{canvas_id}", headers=headers)
        after = len(r.json()["data"].get("blocks") or [])
        print(f"[{args.label}] SSE 关键事件：")
        for t, d in events:
            print(f"  {t}: {d}")
        print(f"[{args.label}] 清空后 DB blocks={after}")
        print(f"[{args.label}] SESSION={sid}")
        return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="ON")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
