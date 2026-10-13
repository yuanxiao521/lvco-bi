# -*- coding: utf-8 -*-
"""date 序列化修复验证：跑一轮按月趋势分析（必然产生 date 型 rows），确认不再崩溃。"""
import asyncio, json, uuid, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import select
from app.core.security import create_access_token
from app.core.database import async_session_factory
from app.models.user import User
from app.models.canvas import Canvas

BASE = 'http://127.0.0.1:8000/api/v1'
DS = '970d798d-f5da-4442-8d25-3563020d5a29'

async def main():
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == 'test@lvco.bi'))).scalar_one()
        canvas = Canvas(user_id=user.id, title='date修复验证-可删')
        db.add(canvas)
        await db.commit()
        cid = canvas.id
    token = create_access_token(str(user.id))
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'text/event-stream'}
    async with httpx.AsyncClient() as c:
        payload = {'canvasId': str(cid), 'datasourceId': str(DS), 'message': '按月统计2024年7月到9月的订单金额趋势，画成折线图'}
        done, err = False, None
        async with c.stream('POST', f'{BASE}/ai/canvas/chat', json=payload, headers=headers, timeout=600.0) as resp:
            async for raw in resp.aiter_lines():
                if raw.startswith('data:'):
                    try:
                        d = json.loads(raw[5:].strip())
                    except Exception:
                        continue
                    if d.get('type') == 'error':
                        err = d.get('message')
                    if d.get('type') == 'done':
                        done = True
    async with async_session_factory() as db:
        row = (await db.execute(select(Canvas).where(Canvas.id == cid))).scalar_one()
        blocks = row.blocks if isinstance(row.blocks, list) else []
        chart_blocks = [b for b in blocks if b.get('type') == 'chart']
        print(f'RESULT done={done} error={err} blocks={len(blocks)} chart_blocks={len(chart_blocks)}')
        if chart_blocks:
            # 抽查块内 rows 是否还有 date 对象形态（应为 ISO 字符串）
            sample = json.dumps(chart_blocks[0], ensure_ascii=False)[:200]
            print('chart block head:', sample)
        await db.delete(row)
        await db.commit()
        print('（验证画布已删）')

asyncio.run(main())
