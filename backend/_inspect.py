import asyncio, sys, collections
sys.path.insert(0, '.')

async def main():
    from tests.agent_evals.run_eval import run_agent
    DS = '970d798d-f5da-4442-8d25-3563020d5a29'
    attempt = await run_agent(
        {'id': 'REPRO', 'category': 'canvas', 'query': '做一个2024年销售分析报告，包含各月销售趋势、各地区销售额对比和品类占比，并配文字解读'},
        user_id='2aace5b5-57d4-42d6-b90d-8d3e9b97ecb1',
        mode='real', entry='canvas', selected_datasource_id=DS,
    )
    types = collections.Counter(e.get('type') for e in attempt.events)
    print('EVENTS:', dict(types))
    print('calls:', attempt.iteration_count)
    for e in attempt.events:
        t = e.get('type')
        if t in ('tool_call','tool_result','text','error'):
            s = str(e)[:150]
            print(t, '|', s)

asyncio.run(main())