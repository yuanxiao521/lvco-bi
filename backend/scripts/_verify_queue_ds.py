# -*- coding: utf-8 -*-
"""审查后复验：队列模式 + 带数据源（UUID 进 payload）+ 真取数。"""
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
    from app.worker import worker_alive
    print('TASK_QUEUE_ENABLED =', settings.TASK_QUEUE_ENABLED, '| worker_alive =', await worker_alive())
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
    token = create_access_token(str(user.id))
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{BASE}/ai/sessions', json={'title': '审查复验-队列+数据源'}, headers={'Authorization': f'Bearer {token}'}, timeout=30)
        sid = r.json()['data']['id']
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'text/event-stream'}
        payload = {'session_id': sid, 'datasource_id': DS, 'message': '各地区销售额分别是多少？'}
        t0 = time.time()
        types, text = [], ''
        err = None
        async with c.stream('POST', f'{BASE}/ai/chat/stream', json=payload, headers=headers, timeout=300) as resp:
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
                if ev.get('type') == 'error':
                    err = ev.get('message')
                if ev.get('type') == 'done':
                    break
        print(f'耗时 {time.time()-t0:.1f}s')
        print('类型:', {t: types.count(t) for t in sorted(set(types))})
        print('文本:', text[:120].replace(chr(10), ' '))
        print('error:', err)
        print('含 done:', 'done' in types)

asyncio.run(main())
