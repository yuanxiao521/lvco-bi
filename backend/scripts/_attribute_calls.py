# -*- coding: utf-8 -*-
"""从评测事件日志做「按内核/按工具」的调用归因（离线，不调 LLM）。

背景：双跑对拍显示单 Agent 腿调用次数比 legacy 多 ~32%，但不知道多在哪。
调用总数由评测记录里的 iteration_count 给出；派发要落在哪个内核记在
run_analysis 的 tool_call args.mode 上。本脚本把这些事件按腿聚合，
用于定位「多出来的调用是 Lead 层的、还是某个内核的」。
"""
import collections
import glob
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'eval_logs', 'agent_evals_archive')
MIN_BYTES = 5000   # 跳过只有 1~2 题的失败重试日志


def load(prefix):
    qs = {}
    for p in sorted(glob.glob(os.path.join(ROOT, f'semantic_events_{prefix}*.jsonl'))):
        if os.path.getsize(p) < MIN_BYTES:
            continue
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                q = json.loads(line)
            except Exception:
                continue
            if q.get('question_id'):
                qs[q['question_id']] = q
    return list(qs.values())


def summarize(name, qs):
    if not qs:
        print(f'{name}: 无数据')
        return
    calls = [q.get('iteration_count') or 0 for q in qs]
    tools = collections.Counter()
    modes = collections.Counter()
    actions = collections.Counter()
    dispatches = 0
    for q in qs:
        for e in q.get('events') or []:
            t = e.get('type')
            if t == 'tool_call':
                n = e.get('name')
                tools[n] += 1
                if n == 'run_analysis':
                    dispatches += 1
                    modes[((e.get('args') or {}).get('mode'))] += 1
            elif t == 'decision':
                actions[e.get('action')] += 1
    print(f'== {name} ==  题数={len(qs)}  总调用={sum(calls)}  平均={sum(calls)/len(qs):.2f}')
    print('  派发次数=', dispatches, ' 按内核=', dict(modes))
    print('  decision=', dict(actions))
    print('  tool_call=', dict(tools.most_common(10)))
    per_kernel = {m: (n, round(n / len(qs), 2)) for m, n in modes.items()}
    print('  内核/题=', per_kernel)
    return dict(questions=len(qs), calls=sum(calls), dispatches=dispatches,
                modes=dict(modes), tools=dict(tools), actions=dict(actions))



# ── 批处理效率：每轮 LLM 实际执行几个工具（决定 ReAct 的调用成本）──────────
LEAD_TOOLS = {
    'get_canvas_layout', 'list_datasources', 'list_fields', 'list_metrics',
    'query_sample', 'run_analysis', 'dispatch_analysis', 'repair',
    'answer', 'ask_user', 'stop',
}


def batching(name, qs):
    """kernel 侧：工具数 / 轮数 ≈ 每轮真实执行的工具数。"""
    if not qs:
        return
    calls = sum(q.get('iteration_count') or 0 for q in qs)
    lead_tools = 0
    kernel_tools = 0
    dispatches = 0
    for q in qs:
        for e in q.get('events') or []:
            if e.get('type') != 'tool_call':
                continue
            n = e.get('name')
            if n in LEAD_TOOLS:
                lead_tools += 1
                if n in ('run_analysis', 'dispatch_analysis', 'repair'):
                    dispatches += 1
            else:
                kernel_tools += 1
    kernel_rounds = max(1, calls - lead_tools)
    print(f'== {name} 批处理 ==')
    print(f'  LLM 轮总数={calls}  Lead 工具={lead_tools}  内核工具={kernel_tools}  派发={dispatches}')
    print(f'  内核轮≈{kernel_rounds}  → 每轮执行内核工具 = {kernel_tools / kernel_rounds:.2f} 个')
    print(f'  每次派发内核工具 = {kernel_tools / max(1, dispatches):.2f} 个')


if __name__ == '__main__':
    for label, prefix in (('legacy', 'l'), ('single_agent', 'u')):
        batching(label, load(prefix))
        print()