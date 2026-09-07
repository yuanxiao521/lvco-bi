"""复现 canvas/chat 事件流：统计事件类型，抓看是否发送过 'message'（text）delta。
作为 Phase 1 根因取证。需要 test@lvco.bi / 123456。
"""
from __future__ import annotations
import json
import re
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL = "test@lvco.bi"
PASSWORD = "123456"


def login():
    r = requests.post(
        f"{BASE}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
        **NO_PROXY, timeout=15,
    )
    print("login status", r.status_code)
    body = r.json()
    if r.status_code != 200:
        print("login failed:", body)
        sys.exit(1)
    return body["data"]["accessToken"] if "data" in body else body["access_token"]


def pick_datasource(token):
    r = requests.get(f"{BASE}/datasources?page=1&page_size=10", headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
    print("ds status", r.status_code)
    body = r.json()
    items = body.get("data", {}).get("items") or body.get("data") or []
    if not items:
        print("no datasource, body=", body)
        sys.exit(1)
    ds = items[0]
    print("pick ds:", ds.get("id"), ds.get("name"))
    return ds


def main():
    token = login()
    ds = pick_datasource(token)

    payload = {
        "datasource_id": ds["id"],
        "session_id": None,
        "message": f"根据 {ds.get('name','当前数据源')} 的字段，做一个TOP10数据分析（比如销量最高的十个区域），并生成柱状图和简短结论",
        "canvas_context": {"availableFields": (ds.get("schemaMeta") or {}).get("fields") or (ds.get("schema_meta") or {}).get("fields") or []},
    }

    print("=== canvas/chat stream ===")
    types: dict[str, int] = {}
    text_total = 0
    started = time.time()
    line = 0
    timeout = 180
    with requests.post(
        f"{BASE}/ai/canvas/chat",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"},
        json=payload, stream=True, **NO_PROXY, timeout=(10, timeout),
    ) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:1500])
            return
        buf = ""
        for raw in r.iter_content(chunk_size=1024, decode_unicode=True):
            if time.time() - started > timeout:
                print("\n!!! TIMEOUT"); break
            if not raw: continue
            buf += raw
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                if not ln.startswith("data: "): continue
                js = ln[6:].strip()
                if not js: continue
                line += 1
                try:
                    ev = json.loads(js)
                except Exception as e:
                    print("BADLINE", line, js[:120], "err", e)
                    continue
                t = ev.get("type")
                types[t] = types.get(t, 0) + 1
                if t == "message":
                    d = ev.get("delta", "")
                    text_total += len(d)
                    print(".", end="", flush=True)
                elif t in ("tool_call", "tool_result", "done", "error", "canvas_action", "step"):
                    # 简写：类型+计数，tool_result 前80字
                    extra = ""
                    if t == "tool_result":
                        r0 = ev.get("result", "") or ""
                        try: r0 = json.loads(r0)
                        except Exception: pass
                        if isinstance(r0, dict):
                            r0 = " ".join(f"{k}={str(v)[:60]}" for k,v in list(r0.items())[:2])
                        else:
                            r0 = str(r0)[:80]
                        extra = f"  <- {r0}"
                    if t == "tool_call":
                        extra = f"  <- name={ev.get('name')} args_keys={list((ev.get('args') or {}).keys())}"
                    print(f"\n[{time.time()-started:.1f}s] {t}{extra}")
                else:
                    print(f"\n[{time.time()-started:.1f}s] OTHER type={t}", json.dumps(ev, ensure_ascii=False)[:120])
    print(f"\n=== 事件计数（总共{line}行）===")
    for k, v in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} x {v}")
    print(f"  message (text) 累计字符数: {text_total}")
    if text_total == 0:
        print("!! ROOT CAUSE CONFIRMED: 后端 canvas/chat 从未发出 message/text 事件（message字符数=0），前端assistantContent=空，显示「思考中...」！")


if __name__ == "__main__":
    main()
