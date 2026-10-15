# -*- coding: utf-8 -*-
"""起一个画布任务并**立刻断线**，模拟"任务在后台跑、用户刷新了页面"。

把 token / canvas_id / session_id 写到 _canvas_task_ids.json，供前端截图脚本使用。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.database import async_session_factory  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.models.user import User  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
DS = "970d798d-f5da-4442-8d25-3563020d5a29"
OUT = Path(__file__).resolve().parents[1] / "_canvas_task_ids.json"


async def main() -> None:
    async with async_session_factory() as db:
        u = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one()
    token = create_access_token(str(u.id))
    auth = {"Authorization": f"Bearer {token}"}
    sse = {**auth, "Accept": "text/event-stream"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0)) as c:
        r = await c.post(f"{BASE}/canvases", headers=auth,
                         json={"title": "画布续收截图", "datasource_id": DS}, timeout=30)
        cid = r.json()["data"]["id"]
        payload = {
            "datasource_id": DS, "session_id": None,
            # 故意要求多产物 + 详细分析 → 这一轮足够长，方便"中途刷新"
            "message": "查询各地区销售额，生成柱状图和饼图，并对销售分布写一段详细的文字分析",
            "canvas_id": cid, "new_session": True,
        }
        sid, n = None, 0
        async with c.stream("POST", f"{BASE}/ai/canvas/chat", json=payload,
                            headers=sse, timeout=600) as resp:
            async for raw in resp.aiter_lines():
                if not raw.startswith("data: "):
                    continue
                try:
                    ev = json.loads(raw[6:].strip())
                except Exception:
                    continue
                n += 1
                if ev.get("type") == "session_created":
                    sid = ev.get("session_id") or (ev.get("session") or {}).get("id")
                if sid and n >= 4:
                    break  # 断开连接；后端任务继续跑

    OUT.write_text(json.dumps({"token": token, "canvas_id": cid, "session_id": sid}),
                   encoding="utf-8")
    print(json.dumps({"canvas_id": cid, "session_id": sid, "events_before_disconnect": n}))


if __name__ == "__main__":
    asyncio.run(main())
