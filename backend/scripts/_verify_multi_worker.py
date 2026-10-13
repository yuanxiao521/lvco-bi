# -*- coding: utf-8 -*-
"""场景 1：多 worker 并发 —— 两个会话同时投递，各自独立完成（不串、不重复）。"""
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import select
from app.core.security import create_access_token
from app.core.database import async_session_factory
from app.models.user import User

BASE = 'http://127.0.0.1:8000/api/v1'


async def 跑一轮(client, token, sid, msg):
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'text/event-stream'}
    payload = {'session_id': sid, 'message': msg}
    t0 = time.time()
    types, text = [], ''
    async with client.stream('POST', f'{BASE}/ai/chat/stream', json=payload, headers=headers, timeout=300) as resp:
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
    return {'耗时': round(time.time() - t0, 1), 'done': 'done' in types, '文本': text[:40].replace(chr(10), ' ')}


async def main():
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
    token = create_access_token(str(user.id))
    async with httpx.AsyncClient() as c:
        sids = []
        for i in (1, 2):
            r = await c.post(f'{BASE}/ai/sessions', json={'title': f'并发验证{i}'}, headers={'Authorization': f'Bearer {token}'}, timeout=30)
            sids.append(r.json()['data']['id'])
        print('两个会话:', [s[:8] for s in sids])
        t0 = time.time()
        results = await asyncio.gather(
            *[跑一轮(c, token, sid, f'你好，我是第{i}个并发请求，请用一句话回应') for i, sid in enumerate(sids, 1)]
        )
        print(f'总墙钟耗时 {time.time()-t0:.1f}s（并发，应远小于两者之和）')
        for i, r in enumerate(results, 1):
            print(f'  会话{i}: {r}')
        print('两个都完成:', all(r['done'] for r in results))

asyncio.run(main())
