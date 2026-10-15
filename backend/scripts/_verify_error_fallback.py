# -*- coding: utf-8 -*-
"""真机验证 P1/P2：任务级异常时
   P1 → SSE 必须**立即**收到 done 并结束（不是等 max_idle 兜底）
   P2 → /sessions 必须显示 interrupted=True / running=False（不是停在 running）
"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import select
from app.core.security import create_access_token
from app.core.database import async_session_factory
from app.models.user import User

BASE = 'http://127.0.0.1:8000/api/v1'
DS = '970d798d-f5da-4442-8d25-3563020d5a29'


async def main():
    from app.config import settings
    print(f"TASK_QUEUE_ENABLED={settings.TASK_QUEUE_ENABLED} key={settings.openai_api_key[:12]}...")
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
    token = create_access_token(str(user.id))
    H = {'Authorization': f'Bearer {token}'}

    async with httpx.AsyncClient() as c:
        r = await c.post(f'{BASE}/ai/sessions', json={'title': '异常兜底验证'}, headers=H, timeout=30)
        sid = r.json()['data']['id']
        print(f"会话 = {sid[:8]}")

        headers = {**H, 'Accept': 'text/event-stream'}
        payload = {'session_id': sid, 'datasource_id': DS, 'message': '各地区销售额分别是多少？'}
        t0 = time.time()
        types, saw_done = [], False
        async with c.stream('POST', f'{BASE}/ai/chat/stream', json=payload,
                            headers=headers, timeout=200) as resp:
            async for raw in resp.aiter_lines():
                if not raw.startswith('data:'):
                    continue
                try:
                    ev = json.loads(raw[5:].strip())
                except Exception:
                    continue
                types.append(ev.get('type'))
                if ev.get('type') == 'done':
                    saw_done = True
                    break
        elapsed = time.time() - t0
        print(f"SSE 结束耗时: {elapsed:.1f}s  | 收到 done: {saw_done}")
        print(f"事件类型: {sorted(set(types))}")

        await asyncio.sleep(2.0)
        rr = await c.get(f'{BASE}/ai/sessions', params={'entry': 'chat'}, headers=H, timeout=15)
        row = next((x for x in rr.json()['data'] if x['id'] == sid), None)
        print(f"完成后 /sessions: running={row.get('running')} interrupted={row.get('interrupted')}")

        ok_p1 = saw_done and elapsed < 60
        ok_p2 = row.get('running') is False and row.get('interrupted') is True
        print()
        print(f"P1（立即 done，不空等）: {'✅' if ok_p1 else '❌'}")
        print(f"P2（写 interrupted 不停在 running）: {'✅' if ok_p2 else '❌'}")
        return 0 if (ok_p1 and ok_p2) else 1


sys.exit(asyncio.run(main()))
