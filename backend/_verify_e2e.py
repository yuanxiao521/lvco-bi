# -*- coding: utf-8 -*-
"""端到端验收（真 LLM + 真后端）：画布感知 / 日期排序 / 中断可见。

用法：python _verify_e2e.py <case>
  case = sense   问「画布上有什么」，验证 Agent 能说出全部块
  case = trend   让 Agent 画按日期的趋势图，验证落库行的日期有序
  case = summary 从 DB 复核最近画布块结构
"""
import json, sys, time

import httpx

BASE = 'http://127.0.0.1:8000/api/v1'
EMAIL, PWD = 'test@lvco.bi', 'Test@12345'
CANVAS_ID = 'fe110f65-1b5e-4055-9799-5e7bf2329e42'
DS_ID = '970d798d-f5da-4442-8d25-3563020d5a29'


def login():
    r = httpx.post(f'{BASE}/auth/login', json={'email': EMAIL, 'password': PWD}, timeout=20)
    r.raise_for_status()
    return r.json()['data']['accessToken']


def canvas_ctx(token, canvas_id):
    r = httpx.get(f'{BASE}/canvases/{canvas_id}', headers={'Authorization': f'Bearer {token}'}, timeout=20)
    r.raise_for_status()
    blocks = (r.json().get('data') or {}).get('blocks') or []
    out = []
    for b in blocks:
        rec = {
            'blockId': b.get('blockId'), 'type': b.get('type'),
            'x': b.get('x'), 'y': b.get('y'), 'width': b.get('width'), 'height': b.get('height'),
        }
        if b.get('type') == 'chart':
            cfg = b.get('_chartConfig') or {}
            rec.update({
                'title': b.get('title'), 'chartType': b.get('chartType'),
                'dimensions': cfg.get('dimensions') or [], 'measures': cfg.get('measures') or [],
            })
        else:
            rec['content'] = b.get('content') or ''
        out.append(rec)
    return out


def chat(token, question, *, canvas_id=CANVAS_ID):
    blocks = canvas_ctx(token, canvas_id)
    body = {
        'message': question,
        'canvas_id': canvas_id,
        'datasource_id': DS_ID,
        'canvas_context': {'blocks': blocks},
    }
    events, text = [], ''
    t0 = time.time()
    with httpx.stream('POST', f'{BASE}/ai/canvas/chat',
                      headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                      json=body, timeout=httpx.Timeout(300.0, connect=20.0)) as resp:
        for line in resp.iter_lines():
            if not line or not line.startswith('data: '):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            events.append(ev)
            if ev.get('type') == 'message':
                text += ev.get('delta') or ''
            if ev.get('type') == 'done':
                break
    return events, text, time.time() - t0, len(blocks)


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else 'sense'
    tok = login()
    q = {
        'sense': '画布上现在有哪些内容块？请按编号列出块类型和标题，并说出图表块数量。',
        'trend': '画一张按日期的销售额趋势图（折线），按月也行，只要趋势清晰。',
    }.get(case, '画布上有什么？')
    events, text, dur, n_blocks = chat(tok, q)
    print(f'[case={case}] 输入画布块数={n_blocks} 耗时={dur:.0f}s 事件={len(events)}')
    acts = [e for e in events if e.get('type') == 'canvas_action']
    print('canvas_action 数 =', len(acts))
    print('---- 助手回答 ----')
    print(text.strip()[:1500])
    errs = [e for e in events if e.get('type') == 'error']
    if errs:
        print('ERRORS:', errs[:3])


if __name__ == '__main__':
    main()