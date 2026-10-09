#demo1
# import json 

# def compact_result_json (result_str, max_chars= 100):
#     if not result_str or len(result_str) <= max_chars: 
#         return result_str
#     obj  = json.loads(result_str)
#     if "error" in obj:
#         return reuslt_str
#     out = {}    
#     for k,v in obj.items():
#         if k =="rows" and isinstance(v,list):
#             out["rows"] = v[:3]
#             out["row_total"] = len(v)
#         else:
#             out[k] = v
#         return json.dumps(out, ensure_ascii=False)
    
# # 测试：构造一个 100 行的查询结果
# result = json.dumps({
#     "columns": ["region", "amount"],
#     "rows": [[f"区域{i}", i * 100] for i in range(100)],
# })
# print("原始长度:", len(result))
# compacted = result
# print("压缩后长度:", len(compacted))
# print("压缩后内容:", compacted[:100], "...")


#demo2
# def compress_history(messages, keep=3):
#     if len(messages) <= keep:
#         return messages
#     head = [messages[0]] if messages[0].get("role") == "system" else []
#     rest = messages[len(head):]
#     dropped = len(rest) - keep
#     kept = rest[-keep:]
#     digest = {"role": "user", "content": f"（较早的 {dropped} 条对话已省略）"}
#     return head + [digest] + kept

# # 测试：10 条消息，只留最近 3 条
# msgs = [{"role": "system", "content": "sys"}] + [
#     {"role": "user" if i % 2 == 0 else "assistant", "content": f"消息{i}"}
#     for i in range(10)
# ]
# out = compress_history(msgs, keep=3)
# print("压缩前:", len(msgs), "条")
# print("压缩后:", len(out), "条")
# for m in out:
#     print("  ", m["role"], "|", m["content"])

#
def find_boundary(messages, keep_rounds):
    found = 0
    boundary = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant" and found < keep_rounds:
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "user":
                    found += 1
                    boundary = j
                    break
        if found >= keep_rounds:
            break
    return boundary

msgs = [{"role": r, "content": f"{c}"} for r, c in [
    ("user", "q1"), ("assistant", "a1"),
    ("user", "q2"), ("assistant", "a2"),
    ("user", "q3"), ("assistant", "a3"),
]]
b = find_boundary(msgs, keep_rounds=2)
print("boundary =", b)
print("保留:", [(m["role"], m["content"]) for m in msgs[b:]])
print("压缩:", [(m["role"], m["content"]) for m in msgs[:b]])