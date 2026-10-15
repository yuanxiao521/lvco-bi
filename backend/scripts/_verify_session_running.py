# -*- coding: utf-8 -*-
"""真机验证：任务执行中 /ai/sessions 的 running 是否为 True（自动续收的触发条件）。

用法：python scripts/_verify_session_running.py
前置：后端已启动（.env 的 TASK_QUEUE_ENABLED 决定模式；arq 模式需同时起 worker）
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
    mode = "arq(队列)" if settings.TASK_QUEUE_ENABLED else "进程内"
    print(f"TASK_QUEUE_ENABLED = {settings.TASK_QUEUE_ENABLED}  →  {mode} 模式")
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
    token = create_access_token(str(user.id))
    H = {'Authorization': f'Bearer {token}'}

    async with httpx.AsyncClient() as c:
        r = await c.post(f'{BASE}/ai/sessions', json={'title': 'running字段验证'}, headers=H, timeout=30)
        sid = r.json()['data']['id']
        print(f"会话 = {sid[:8]}")

        async def send():
            headers = {**H, 'Accept': 'text/event-stream'}
            payload = {'session_id': sid, 'datasource_id': DS,
                       'message': '各地区销售额分别是多少？请给出详细分析'}
            t0 = time.time()
            async with c.stream('POST', f'{BASE}/ai/chat/stream', json=payload,
                                headers=headers, timeout=300) as resp:
                async for raw in resp.aiter_lines():
                    if raw.startswith('data:') and '"done"' in raw:
                        break
            return time.time() - t0

        task = asyncio.create_task(send())

        # 轮询 /ai/sessions，记录 running 的变化
        samples, seen_true, false_after_done = [], False, None
        while not task.done():
            rr = await c.get(f'{BASE}/ai/sessions', params={'entry': 'chat'}, headers=H, timeout=15)
            for item in rr.json()['data']:
                if item['id'] == sid:
                    run = item.get('running')
                    intr = item.get('interrupted')
                    samples.append((round(time.time(), 1), run, intr))
                    if run is True:
                        seen_true = True
            await asyncio.sleep(1.0)

        elapsed = await task
        await asyncio.sleep(1.5)
        rr = await c.get(f'{BASE}/ai/sessions', params={'entry': 'chat'}, headers=H, timeout=15)
        for item in rr.json()['data']:
            if item['id'] == sid:
                false_after_done = (item.get('running'), item.get('interrupted'))

        print(f"任务耗时 {elapsed:.1f}s")
        running_vals = [s[1] for s in samples]
        print(f"轮询 {len(samples)} 次 | running 序列: {running_vals}")
        print(f"执行期间出现过 running=True : {seen_true}")
        print(f"完成后再查 (running, interrupted) = {false_after_done}")
        ok = seen_true and false_after_done == (False, False)
        print()
        print("结论:", "✅ 通过" if ok else "❌ 失败")
        return 0 if ok else 1


sys.exit(asyncio.run(main()))
