"""Phase 4 验证脚本：跑完 canvas/chat 后，GET /sessions/{sid}/messages 确认 assistant 消息入库，
  即便 full_content 为空也应该有兜底文案被保存。
"""
from __future__ import annotations
import json
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL = "test@lvco.bi"
PASSWORD = "123456"


def login():
    r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD}, **NO_PROXY, timeout=15)
    body = r.json()
    assert r.status_code == 200, body
    return body["data"]["accessToken"] if "data" in body else body["access_token"]


def pick_ds(token):
    r = requests.get(f"{BASE}/datasources?page=1&page_size=10", headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
    body = r.json()
    items = body.get("data", {}).get("items") or body.get("data") or []
    assert items, ("no datasource", body)
    return items[0]


def run_canvas(token, ds):
    payload = {
        "datasource_id": ds["id"],
        "session_id": None,
        "message": "先查询数据，再根据查询结果在画布生成TOP10柱状图，以及一个简短文本标题",
        "canvas_context": {
            "availableFields": ((ds.get("schemaMeta") or {}).get("fields") or (ds.get("schema_meta") or {}).get("fields") or [])[:15]
        },
    }
    session_id = None
    last_err = None
    with requests.post(
        f"{BASE}/ai/canvas/chat",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"},
        json=payload, stream=True, **NO_PROXY, timeout=(10, 240),
    ) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:1500])
            sys.exit(2)
        buf = ""
        for raw in r.iter_content(chunk_size=1024, decode_unicode=True):
            if not raw: continue
            buf += raw
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                if not ln.startswith("data: "): continue
                js = ln[6:].strip()
                if not js: continue
                try:
                    ev = json.loads(js)
                except Exception as e:
                    last_err = f"parse {js[:60]} err {e}"
                    continue
                t = ev.get("type")
                if t == "session_created":
                    session_id = ev.get("session_id") or ((ev.get("session") or {}).get("id") or (ev.get("session") or {}).get("sessionId"))
                    print("SESSION:", session_id)
    return session_id, last_err


def list_messages(token, sid):
    r = requests.get(f"{BASE}/ai/sessions/{sid}/messages", headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
    print("list ms status", r.status_code)
    body = r.json()
    # 兼容：直接数组 / {data:items} / {data:{items:...}} / {messages:[...]}
    if isinstance(body, list):
        items = body
    else:
        d = body.get("data")
        if isinstance(d, list):
            items = d
        elif isinstance(d, dict):
            items = d.get("items") or d.get("messages") or []
        else:
            items = body.get("messages") or body.get("items") or []
    roles = [m.get("role") or m.get("roleName") for m in items]
    print("roles:", roles)
    for m in items:
        role = m.get("role") or m.get("roleName")
        c = (m.get("content") or "")[:80].replace("\n", "\\n")
        print(f"  - [{role}] {c}")
    return items


def main():
    token = login()
    ds = pick_ds(token)
    print("DS:", ds["id"], ds.get("name"))
    sid, err = run_canvas(token, ds)
    if err: print("WARN stream err:", err)
    assert sid, "no session id from stream!"
    # 给 DB 留 commit 时间
    time.sleep(0.8)
    msgs = list_messages(token, sid)
    has_user = any((m.get("role") or m.get("roleName")) == "user" for m in msgs)
    has_assistant = any((m.get("role") or m.get("roleName")) == "assistant" for m in msgs)
    print(f"\nVERIFY: has_user={has_user} has_assistant={has_assistant} total={len(msgs)}")
    if has_assistant:
        a = next(m for m in msgs if (m.get("role") or m.get("roleName")) == "assistant")
        print("ASSISTANT:", (a.get("content") or "")[:200])
    assert has_assistant, "!!! ROOT BUG STILL HERE: assistant 未入库，下次进入历史就只剩用户消息刷屏！"
    print("\n[PASS] canvas_chat assistant 兜底入库生效 ✓")


if __name__ == "__main__":
    main()
