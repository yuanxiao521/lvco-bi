# -*- coding: utf-8 -*-
"""第 1 课：为什么需要"事件日志"？

问题场景
--------
用户点了"生成分析报告"，服务器一边跑一边把进度推给浏览器：
    [进度] 正在查询数据... → [进度] 生成图表... → [完成]

如果浏览器在"生成图表"时刷新了，会发生什么？
这取决于**服务器怎么存这些事件**。本课用两种最朴素的方式对比。

跑法：.venv/Scripts/python.exe learning/01_why_log.py
"""
import queue

print("=" * 60)
print("方式 A：破坏性读取（读完就删）—— 相当于 Redis 的 LPOP")
print("=" * 60)

mailbox = queue.Queue()
for i in range(1, 6):
    mailbox.put(f"事件{i}")          # 生产者写了 5 个事件

read = []
for _ in range(2):
    read.append(mailbox.get())        # 读者读了 2 个（模拟刷新前）
print(f"刷新前读者收到：{read}")

# 读者刷新了，剩下的还在队列里
remaining = []
while not mailbox.empty():
    remaining.append(mailbox.get())
print(f"刷新后队列里剩：{remaining}")
print("→ 读者错过了这 3 个事件，而且**它们已经不在队列里了**（被读走了就没了）")

print()
print("=" * 60)
print("方式 B：日志（只追加，不删除）—— 相当于 Redis 的 XADD/XRANGE")
print("=" * 60)

log = []                              # 只追加，永不删除
for i in range(1, 6):
    log.append({"id": f"{i}-0", "data": f"事件{i}"})

read = log[:2]                        # 读者按"游标"读了前 2 条
print(f"刷新前读者收到：{[e['data'] for e in read]}")
cursor = read[-1]["id"]
print(f"读者的游标停在：{cursor}")

print(f"日志里仍然有 {len(log)} 条：{[e['data'] for e in log]}")
missed = [e for e in log if e["id"] > cursor]
print(f"刷新后从游标之后读：{[e['data'] for e in missed]}")
print("→ 一条不差地补回来了 ✓")

print()
print("结论：")
print("  事件必须**持久化 + 有编号**，读者自己记住游标，才能断线续传。")
print("  这就是 Redis Stream 的设计出发点。")
