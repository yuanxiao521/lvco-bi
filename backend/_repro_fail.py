# -*- coding: utf-8 -*-
import asyncio, sys
sys.path.insert(0, '.')

async def main():
    from tests.agent_evals.run_eval import run_agent
    DS = '970d798d-f5da-4442-8d25-3563020d5a29'
    attempt = await run_agent(
        {'id': 'REPRO', 'category': 'canvas', 'query': '做一个2024年销售分析报告，包含各月销售趋势、各地区销售额对比和品类占比，并配文字解读'},
        user_id='2aace5b5-57d4-42d6-b90d-8d3e9b97ecb1',
        mode='real', entry='canvas', selected_datasource_id=DS,
    )
    fails = 0
    for e in attempt.events:
        if e.get('type') == 'tool_result' and e.get('ok') is False:
            fails += 1
            print('FAIL#', fails, e.get('name'), str(e.get('result'))[:200])
    print('total tool_result fails =', fails)
    blocks = sum(1 for e in attempt.events if e.get('type')=='canvas_action' and str((e.get('action') or {}).get('action','')).startswith('add_'))
    print('canvas add actions =', blocks)
    print('error:', attempt.error)

asyncio.run(main())