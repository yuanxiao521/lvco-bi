import asyncio, json, pathlib, sys
sys.path.insert(0, str(pathlib.Path('.').resolve()))
from tests.agent_evals.run_eval import build_sql_executor, _resolve_datasource_id
from tests.agent_evals.judge import AttemptTrace, judge_attempt, aggregate_results

base = pathlib.Path('tests/agent_evals')
dataset = [json.loads(l) for l in (base/'dataset.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
events = [json.loads(l) for l in (base/'eval_results_events.jsonl').read_text(encoding='utf-8').splitlines() if l.strip()]
by_id = {e['question_id']: e for e in events}
USER = '21bee02f-dcb3-4108-b721-d8448db678e4'

def final_text(evs):
    txt = ''
    for e in evs:
        if e.get('type') in ('report','text') and e.get('content'):
            txt = e['content'] if e.get('type')=='report' else txt + e['content']
    return txt

async def main():
    rows = []
    for q in dataset[:12]:
        rec = by_id.get(q['id'])
        if not rec: continue
        evs = rec.get('events') or []
        attempt = AttemptTrace(question_id=q['id'], user_msg=q.get('query',''), events=evs,
                               final_response=final_text(evs), error=rec.get('error'),
                               iteration_count=rec.get('iteration_count') or 0)
        ex = await build_sql_executor(attempt, q.get('expected_datasource'), user_id=USER)
        rows.append(judge_attempt(q, attempt, sql_executor=ex))
    s = aggregate_results(rows)
    print('题目数', s['total'])
    print('SQL 准确率', s['sql_accuracy_count'], '/', s['total'], round(s['sql_accuracy_rate']*100,1), '%')
    print('图表类型正确率', s['chart_type_correct_count'], '/', s['total'], round(s['chart_type_correct_rate']*100,1), '%')
    print('工具成功率', s['tool_success_count'], '/', s['total'])
    print('输出有效率', s['final_response_valid_count'], '/', s['total'])
    for r in rows:
        print(' ', r.question_id, 'sql', int(r.sql_accuracy), 'chart', int(r.chart_type_correct), r.expected_chart_type, '->', r.actual_chart_type, '|', (r.notes[:1] or ['-'])[0][:70])

asyncio.run(main())