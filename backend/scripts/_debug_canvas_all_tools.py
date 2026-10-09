"""在 Phase1 基础上，把所有 tool_call/tool_result 都打印出来，而不仅是 render_chart。"""
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
    r = requests.get(f"{BASE}/datasources?page=1&page_size=20", headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
    body = r.json()
    items = body.get("data", {}).get("items") or body.get("data") or []
    assert items, body
    def fields_of(ds):
        sm = ds.get("schemaMeta") or ds.get("schema_meta") or {}
        fs = sm.get("fields") or []
        return [f.get("name") if isinstance(f, dict) else str(f) for f in fs]
    for d in items:
        names = [str(n).lower() for n in fields_of(d)]
        if any(k in "".join(names) for k in ("fans", "like", "comment", "douyin")):
            return d, fields_of(d)
    return items[0], fields_of(items[0])


def run(token, ds, fields):
    payload = {
        "datasource_id": ds["id"],
        "session_id": None,
        "message": "找出TOP5粉丝数最多的达人，先查询数据，然后把TOP5柱状图添加到画布上。",
        "canvas_context": {"availableFields": [{"name": f, "data_type": "string"} for f in (fields or [])[:25]]},
    }
    events_by_type: dict[str, int] = {}
    session_id = None
    errors: list[str] = []
    canvas_actions: list[dict] = []

    started = time.time()
    with requests.post(
        f"{BASE}/ai/canvas/chat",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"},
        json=payload, stream=True, **NO_PROXY, timeout=(10, 240),
    ) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:2000]); sys.exit(3)
        buf = ""
        last_tool_name: str | None = None
        last_tool_args: dict = {}
        for raw in r.iter_content(chunk_size=1024, decode_unicode=True):
            if time.time() - started > 240: break
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
                    errors.append(f"parse_err {js[:80]} e={e}"); continue
                t = ev.get("type")
                events_by_type[t] = events_by_type.get(t, 0) + 1
                if t == "session_created":
                    session_id = ev.get("session_id") or ((ev.get("session") or {}).get("id") or (ev.get("session") or {}).get("sessionId"))
                elif t == "tool_call":
                    last_tool_name = ev.get("name", "")
                    last_tool_args = dict(ev.get("args", {}) or {})
                    print(f"\n  [{time.time()-started:.1f}s] TOOL_CALL name={last_tool_name}")
                    print(f"    args snippet: {json.dumps(last_tool_args, ensure_ascii=False)[:260]}")
                elif t == "tool_result":
                    name = ev.get("name", "")
                    result_raw = ev.get("result", "")
                    try:
                        r_obj = json.loads(result_raw) if isinstance(result_raw, str) else {}
                        is_err = isinstance(r_obj, dict) and bool(r_obj.get("error"))
                        has_ca = isinstance(r_obj, dict) and isinstance(r_obj.get("canvas_action"), dict)
                        keys = list(r_obj.keys()) if isinstance(r_obj, dict) else [type(r_obj).__name__]
                        print(f"  [{time.time()-started:.1f}s] TOOL_RESULT name={name} error={is_err} has_canvas_action={has_ca} top_keys={keys[:10]}")
                        if is_err:
                            print(f"    error: {(r_obj.get('error') if isinstance(r_obj, dict) else '')[:200]}")
                        if isinstance(r_obj, dict) and r_obj.get("hint"):
                            print(f"    hint: {str(r_obj['hint'])[:150]}")
                    except Exception as e:
                        print(f"  [{time.time()-started:.1f}s] TOOL_RESULT name={name} parse_fail={e} snippet={str(result_raw)[:120]}")
                elif t == "canvas_action":
                    ca = {k: (str(v)[:60] if isinstance(v, (dict, list)) else v) for k, v in list(ev.items())[:6]}
                    canvas_actions.append(ca)
                    print(f"  [{time.time()-started:.1f}s] ✅ CANVAS_ACTION: {json.dumps(ca, ensure_ascii=False)[:200]}")
                elif t == "chart":
                    print(f"  [{time.time()-started:.1f}s] chart event type={ev.get('chart_type')} has_option={bool(ev.get('option'))}")
                elif t == "message":
                    pass  # 不打印 delta 洪流
                elif t == "error":
                    errors.append(f"SSE_error: {ev.get('message','')[:200]}")
                    print(f"  [{time.time()-started:.1f}s] ⚠️ SSE ERROR: {ev.get('message','')[:200]}")

    print("\n=== 事件计数 ===")
    for k, v in sorted(events_by_type.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} x {v}")
    print(f"\n=== canvas_action 总数：{len(canvas_actions)}")
    for i, ca in enumerate(canvas_actions, 1):
        print(f"  #{i}: {json.dumps(ca, ensure_ascii=False)[:240]}")
    if errors:
        print(f"\n=== errors ({len(errors)})")
        for e in errors[:5]:
            print(f"  ! {e}")
    print(f"\n=== session_id = {session_id}")
    if events_by_type.get("tool_call") and not canvas_actions:
        print("\n!! 新现象：白名单限制后 render_chart 消失了，但 canvas_action 仍为 0。需要检查 tool_call 是否改走 add_chart_block 且失败（如缺 datasource_id/dimensions/measures 参数）。")


def main():
    token = login()
    ds, fields = pick_ds(token)
    print("DS:", ds.get("name"), "id=", ds["id"])
    run(token, ds, fields)


if __name__ == "__main__":
    main()
