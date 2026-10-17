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
    # 打印最后 25 个事件的类型与内容摘要（找 stop 之后的 text）
    evs = attempt.events
    stop_idx = max((i for i,e in enumerate(evs) if e.get('type')=='decision' and e.get('action')=='stop'), default=-1)
    print('stop event idx =', stop_idx, ' total events =', len(evs))
    for i, e in enumerate(evs):
        if i >= stop_idx - 2:
            s = str(e)[:130]
            print(i, e.get('type'), '|', s)

asyncio.run(main())