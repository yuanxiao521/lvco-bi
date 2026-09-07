"""验证 SELECT * 修复：
1. 登录 test@lvco.bi
2. 调 canvas_chat 问「统计 douyin 每个 category 的账号数，显示前 5 个」——预期 LLM 生成 SELECT category, COUNT(*) GROUP BY
3. 打印所有 tool_result，看 query_datasource 是否仍然报 SELECT *
"""
import sys, os, json, time, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000/api/v1"
EMAIL, PWD = "test@lvco.bi", "123456"

s = requests.Session()
r = s.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PWD})
r.raise_for_status()
tk = r.json().get("access_token") or r.json()["data"]["access_token"]
s.headers.update({"Authorization": f"Bearer {tk}"})
print(f"[login] {r.status_code}, user_id={r.json().get('data',{}).get('user_id')}")

datasource_id = "1ab854fa-beb5-4909-9470-9d8561d176b1"
msg = "统计 douyin 数据源里每个 category（分类）下的账号数量，按数量降序取前 5 条"

print(f"\n[canvas_chat] query: {msg}")
print("=" * 80)

r = s.post(f"{BASE}/ai/canvas/chat", json={
    "message": msg,
    "session_id": "debug-select-star-" + str(int(time.time())),
    "datasource_ids": [datasource_id],
    "user_id": "2aace5b5-57d4-42d6-b90d-8d3e9b97ecb1",
}, stream=True, timeout=120)
r.raise_for_status()

buf = b""
tool_results = []
canvas_actions = 0
for line in r.iter_lines():
    if not line:
        continue
    if line.startswith(b"data:"):
        line = line[5:].lstrip()
    try:
        evt = json.loads(line.decode("utf-8"))
    except Exception:
        continue
    etype = evt.get("type", evt.get("event"))
    if etype == "tool_result":
        name = evt.get("name")
        rslt = evt.get("result") or "{}"
        try:
            rslt_obj = json.loads(rslt)
        except Exception:
            rslt_obj = {"raw": rslt[:200]}
        tool_results.append({"name": name, "result": rslt_obj})
        tag = " ❌" if rslt_obj.get("error") else " ✅"
        err = rslt_obj.get("error", "")
        snippet = err[:120] if err else f"keys={list(rslt_obj.keys())[:5]}"
        print(f"  tool_result[{name}]{tag}: {snippet}")
    elif etype == "canvas_action":
        canvas_actions += 1
        action = (evt.get("data") or {}).get("action", "?")
        print(f"  canvas_action #{canvas_actions}: {action}")
    elif etype == "error":
        print(f"  ERROR event: {evt}")
    elif etype == "done":
        print("  [DONE]")

print("\n" + "=" * 80)
print(f"汇总：tool_results 共 {len(tool_results)} 条；canvas_actions={canvas_actions}")
for tr in tool_results:
    if tr["name"] == "query_datasource":
        err = tr["result"].get("error")
        print(f"  query_datasource: {'❌ 仍报错: '+err[:200] if err else '✅ 成功 columns='+str(tr['result'].get('columns'))[:100]}")
