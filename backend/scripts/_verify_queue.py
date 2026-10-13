# -*- coding: utf-8 -*-
"""A（ARQ 队列）真机验证：请求经队列由 worker 进程执行，SSE 从事件日志读。"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import select
from app.core.security import create_access_token
from app.core.database import async_session_factory
from app.models.user import User

BASE = 'http://127.0.0.1:8000/api/v1'

async def main():
    from app.config import settings
    print('TASK_QUEUE_ENABLED =', settings.TASK_QUEUE_ENABLED)
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
    token = create_access_token(str(user.id))
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{BASE}/ai/sessions', json={'title': 'A队列验证'}, headers={'Authorization': f'Bearer {token}'}, timeout=30)
        sid = r.json()['data']['id']
        print('session =', sid[:8])
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'text/event-stream'}
        payload = {'session_id': sid, 'message': '你好，请用一句话介绍你自己'}
        t0 = time.time()
        types, text = [], ''
        async with c.stream('POST', f'{BASE}/ai/chat/stream', json=payload, headers=headers, timeout=180) as resp:
            async for raw in resp.aiter_lines():
                if not raw.startswith('data:'):
                    continue
                try:
                    ev = json.loads(raw[5:].strip())
                except Exception:
                    continue
                types.append(ev.get('type'))
                if ev.get('type') == 'message':
                    text += str(ev.get('delta') or '')
                if ev.get('type') == 'done':
                    break
        print(f'耗时 {time.time()-t0:.1f}s')
        print('事件类型:', {t: types.count(t) for t in sorted(set(types))})
        print('文本:', text[:100].replace(chr(10), ' '))
        print('含 done:', 'done' in types)

asyncio.run(main())
