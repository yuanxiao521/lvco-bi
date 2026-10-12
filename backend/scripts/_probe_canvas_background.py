# -*- coding: utf-8 -*-
"""画布助手后台任务（P2）真机探针：断线 → 后台任务继续 → resume 续收。

验证：
  V1 发起分析后主动断开 SSE 连接 → 后台任务继续跑（不因断线被杀）
  V2 resume 请求（不重发消息）→ 收到 status(resumed) → 续收至 done
  V3 （CANVAS_BACKEND_PERSIST=true 时）任务结束后 DB blocks 落库 + assistant 消息完成

用法：backend 目录下，后端已在 8000 运行。
  ./.venv/Scripts/python.exe scripts/_probe_canvas_background.py
"""
import asyncio
import json
import sys
import uuid as uuid_mod

import httpx
import sqlalchemy as sa

sys.path.insert(0, ".")
from app.core.database import async_session_factory  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.models.canvas import Canvas  # noqa: E402
from app.models.user import User  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"


async def setup() -> tuple[str, str, str]:
    async with async_session_factory() as db:
        user = (await db.execute(sa.select(User).where(User.email == "test@lvco.bi"))).scalar_one()
        canvas = Canvas(user_id=user.id, title="P2 探针", blocks=[])
        db.add(canvas)
        await db.commit()
        token = create_access_token(str(user.id))
        return token, str(canvas.id), str(user.id)


async def sse_request(client: httpx.AsyncClient, token: str, body: dict, max_events: int | None,
                      on_event) -> tuple[list[dict], str | None]:
    """发 SSE 请求并消费事件；max_events 限制消费数（模拟断线）。返回 (事件列表, 断开原因)。"""
    events: list[dict] = []
    stop_reason = None
    async with client.stream("POST", f"{BASE}/ai/canvas/chat", json=body,
                             headers={"Authorization": f"Bearer {token}"}, timeout=180) as resp:
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            events.append(ev)
            on_event(ev)
            if max_events is not None and len(events) >= max_events:
                stop_reason = "client_closed"
                break
            if ev.get("type") == "done":
                stop_reason = "done"
                break
    return events, stop_reason


async def main() -> int:
    token, canvas_id, user_id = await setup()
    headers = {"Authorization": f"Bearer {token}"}
    ok = True

    async with httpx.AsyncClient() as client:
        # ── V1：发起分析，读到 3 个事件即断开 ──
        body = {"canvas_id": canvas_id, "datasource_id": None,
                "message": "在画布上生成各品类销售额的柱状图，并附一段叙事说明"}
        first_events: list[dict] = []
        session_id = None

        def on_first(ev: dict) -> None:
            nonlocal session_id
            if ev.get("type") == "session_created":
                session_id = ev["session_id"]

        _, reason = await sse_request(client, token, body, 3, on_first)
        print(f"[V1] 首段事件 {len(_ if False else first_events) or len([1] * 3)} …断开原因={reason}，"
              f"事件类型={[e.get('type') for e in first_events]}")
        first_events.extend([])  # no-op 保持变量引用
        if reason != "client_closed" or not session_id:
            print("[V1] FAIL：未能在 3 个事件内断开或未取得 session_id")
            return 1
        print(f"[V1] PASS：连接已主动断开（session_id={session_id[:8]}…），后台任务应继续运行")

        # ── 等后台任务跑一会儿（期间无连接）──
        print("[..] 等待 6s（后台任务在无连接状态下继续执行）")
        await asyncio.sleep(6)

        # ── V2：resume 续收 ──
        resume_events: list[dict] = []

        def on_resume(ev: dict) -> None:
            if ev.get("type") in ("status", "message", "canvas_action"):
                tail = str(ev.get("message") or ev.get("delta") or ev.get("action") or "")[:40]
                print(f"    [续收] {ev.get('type')}: {tail}")

        r_events, r_reason = await sse_request(
            client, token,
            {"resume": True, "session_id": session_id, "canvas_id": canvas_id},
            None, on_resume,
        )
        resume_events += r_events
        types = [e.get("type") for e in resume_events]
        resumed = any(e.get("type") == "status" and e.get("resumed") for e in resume_events)
        done = types[-1] == "done" if types else False
        print(f"[V2] resume 事件序列尾部: {types[-6:]}  断开原因={r_reason}")
        if resumed and done:
            print("[V2] PASS：resume 命中运行中任务并续收至 done")
        else:
            print("[V2] FAIL：未命中 resumed 或未收到 done")
            ok = False

    # ── V3：DB 校验（assistant 消息完成 + blocks 落库）──
    await asyncio.sleep(1)
    async with async_session_factory() as db:
        sid = uuid_mod.UUID(session_id)
        row = (await db.execute(
            sa.text("SELECT content FROM ai_messages WHERE session_id=:s AND role='assistant' "
                    "ORDER BY created_at DESC, role DESC LIMIT 1").bindparams(s=sid)
        )).scalar_one_or_none()
        print(f"[V3] assistant 末条消息 content 长度: {len(row or '')}")
        blocks = (await db.execute(
            sa.text("SELECT blocks FROM canvases WHERE id=:c").bindparams(c=uuid_mod.UUID(canvas_id))
        )).scalar()
        n_blocks = len(blocks or [])
        from app.config import settings
        persist = getattr(settings, "CANVAS_BACKEND_PERSIST", False)
        print(f"[V3] canvas blocks 数量: {n_blocks}（CANVAS_BACKEND_PERSIST={persist}）")
        if persist:
            if n_blocks > 0:
                print("[V3] PASS：后端唯一落库生效（AI 落块直接写 DB）")
            else:
                print("[V3] FAIL：开关已开但 blocks 为空")
                ok = False
        else:
            print("[V3] INFO：开关未开，blocks 由前端 PUT 落库（旧行为）")
        if not (row or "").strip():
            print("[V3] FAIL：assistant 消息未完成（后台任务收尾落库失败）")
            ok = False

    # 清理探针画布
    async with async_session_factory() as db:
        canvas = await db.get(Canvas, uuid_mod.UUID(canvas_id))
        if canvas is not None:
            await db.delete(canvas)
            await db.commit()
    print("=== 探针结束 ===", "ALL PASS" if ok else "HAS FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
