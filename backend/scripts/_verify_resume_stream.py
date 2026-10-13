# -*- coding: utf-8 -*-
"""验证 B（事件日志续收）：中途断开 → 重连 resume → 回放+实时无缝隙、无重复。

验收点：
1. 断线期间产生的事件，重连后**能看到**（旧实现只有文本，步骤/落块会丢）
2. 回放与实时衔接处**不重复**（游标续读，不是从头重放）
3. 最终能收到 done（任务正常收尾）
"""
import asyncio, json, sys, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import select
from app.core.security import create_access_token
from app.core.database import async_session_factory
from app.models.user import User

BASE = 'http://127.0.0.1:8000/api/v1'


async def read_events(client, token, sid, msg, resume, stop_after=None, max_seconds=90):
    """读一段 SSE；stop_after 给定时读够这么多事件就断开（模拟刷新）。"""
    events = []
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'text/event-stream'}
    payload = {'sessionId': sid, 'message': msg, 'resume': resume}
    t0 = time.time()
    async with client.stream('POST', f'{BASE}/ai/chat/stream', json=payload, headers=headers, timeout=max_seconds) as resp:
        if resp.status_code != 200:
            return events, f'HTTP {resp.status_code}'
        async for raw in resp.aiter_lines():
            if time.time() - t0 > max_seconds:
                return events, 'timeout'
            if not raw.startswith('data:'):
                continue
            try:
                ev = json.loads(raw[5:].strip())
            except Exception:
                continue
            events.append(ev)
            if stop_after is not None and len(events) >= stop_after:
                return events, 'cut'          # 主动断开（模拟刷新）
            if ev.get('type') == 'done':
                return events, 'done'
    return events, 'eof'


async def main():
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
    token = create_access_token(str(user.id))
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{BASE}/ai/sessions', json={'title': 'B续收验证'}, headers={'Authorization': f'Bearer {token}'}, timeout=30)
        sid = r.json()['data']['id']
        print('session =', sid[:8])

        # 1) 发一条会触发分析的请求，读 3 个事件后断开（模拟用户刷新）
        first, how = await read_events(c, token, sid, '各地区销售额分别是多少？画个柱状图', resume=False, stop_after=3)
        print(f'[断开] 收到 {len(first)} 个事件（{how}）:', [e.get('type') for e in first])

        # 2) 断线期间任务继续跑（这是后台任务化的前提）
        await asyncio.sleep(3)

        # 3) 重连续收
        second, how2 = await read_events(c, token, sid, '', resume=True, max_seconds=200)
        types = [e.get('type') for e in second]
        text = ''.join(str(e.get('delta') or '') for e in second if e.get('type') == 'message')
        print(f'[重连] 收到 {len(second)} 个事件（{how2}）')
        print('  类型分布:', {t: types.count(t) for t in sorted(set(types))})
        print('  文本长度:', len(text), '| 文本头:', text[:60].replace(chr(10), ' '))
        print('  含 done:', 'done' in types)

        # 4) 重复性检查：回放段不应把同一条文本发两遍
        msgs = [e for e in second if e.get('type') == 'message']
        print('  message 事件数:', len(msgs), '（回放合并为 1 条 + 实时若干）')

asyncio.run(main())
