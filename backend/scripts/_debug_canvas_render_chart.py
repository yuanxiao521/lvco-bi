"""Phase1 取证脚本：问"TOP5关键指标"类问题，完整记录 render_chart 的 tool_result（是否 ok/含 canvas_action）、
  canvas_action 事件数量和参数。复现"显示完成了，但是没有生成图表"。
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
    r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD}, **NO_PROXY, timeout=15)
    body = r.json()
    assert r.status_code == 200, body
    return body["data"]["accessToken"] if "data" in body else body["access_token"]


def pick_ds(token):
    r = requests.get(f"{BASE}/datasources?page=1&page_size=20", headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
    body = r.json()
    items = body.get("data", {}).get("items") or body.get("data") or []
    assert items, body
    # 优先挑字段含粉丝/点赞/评论/达人（复现用户抖音数据TOP5场景）
    def fields_of(ds):
        sm = ds.get("schemaMeta") or ds.get("schema_meta") or {}
        fs = sm.get("fields") or []
        return [f.get("name") if isinstance(f, dict) else str(f) for f in fs]
    for d in items:
        names = [str(n).lower() for n in fields_of(d)]
        if any(k in "".join(names) for k in ("fans", "like", "comment", "达人", "达人排名", "douyin")):
            print("优先命中:", d.get("name"))
            return d, fields_of(d)
    d = items[0]
    return d, fields_of(d)


def pretty_snippet(s, n=140):
    if s is None: return ""
    s = str(s)
    return s.replace("\n", "\\n")[:n]


def run(token, ds, fields):
    payload = {
        "datasource_id": ds["id"],
        "session_id": None,
        "message": "根据当前数据源字段，帮我找出数据中的TOP5关键指标（比如粉丝数最多、点赞/评论最多），然后生成柱状图对比并在画布上落图表。",
        "canvas_context": {"availableFields": [{"name": f, "data_type": "string"} for f in (fields or [])[:25]]},
    }
    events_by_type: dict[str, int] = {}
    render_chart_results: list[dict] = []
    canvas_actions: list[dict] = []
    session_id = None
    chart_events: list[dict] = []
    errors: list[str] = []

    started = time.time()
    with requests.post(
        f"{BASE}/ai/canvas/chat",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "text/event-stream"},
        json=payload, stream=True, **NO_PROXY, timeout=(10, 240),
    ) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:2000])
            sys.exit(3)
        buf = ""
        for raw in r.iter_content(chunk_size=1024, decode_unicode=True):
            if time.time() - started > 240:
                print("!! TIMEOUT"); break
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
                    errors.append(f"parse_err {js[:80]} e={e}")
                    continue
                t = ev.get("type")
                events_by_type[t] = events_by_type.get(t, 0) + 1
                if t == "session_created":
                    session_id = ev.get("session_id") or ((ev.get("session") or {}).get("id") or (ev.get("session") or {}).get("sessionId"))
                elif t == "tool_call" and ev.get("name") == "render_chart":
                    print(f"\n  [{time.time()-started:.1f}s] tool_call render_chart args={json.dumps(ev.get('args'), ensure_ascii=False)[:200]}")
                elif t == "tool_result" and len(render_chart_results) < events_by_type.get("tool_call", 0):
                    # 记录 tool_result 顺序，对应最近一次 render_chart（近似即可，这里只分析 render_chart 命名的结果）
                    name_raw = ev.get("name", "")
                    try:
                        r_obj = json.loads(ev.get("result", "")) if isinstance(ev.get("result"), str) else ev.get("result")
                    except Exception:
                        r_obj = {"raw": ev.get("result", "")}
                    is_ok = not (isinstance(r_obj, dict) and r_obj.get("error"))
                    has_ca = isinstance(r_obj, dict) and isinstance(r_obj.get("canvas_action"), dict)
                    if name_raw == "render_chart":
                        render_chart_results.append({
                            "ok": is_ok,
                            "has_canvas_action": has_ca,
                            "canvas_action_keys": list((r_obj.get("canvas_action") or {}).keys()) if has_ca else [],
                            "error": r_obj.get("error") if isinstance(r_obj, dict) else None,
                            "other_keys_preview": pretty_snippet(list(r_obj.keys()) if isinstance(r_obj, dict) else type(r_obj).__name__, 120),
                        })
                        print(f"  [{time.time()-started:.1f}s] tool_result render_chart → ok={is_ok} has_ca={has_ca} keys={list(r_obj.keys()) if isinstance(r_obj, dict) else type(r_obj).__name__} err={pretty_snippet(r_obj.get('error') if isinstance(r_obj, dict) else None, 140)}")
                elif t == "canvas_action":
                    ca = {k: (pretty_snippet(v, 60) if isinstance(v, (dict, list)) else v) for k, v in list(ev.items())[:6]}
                    canvas_actions.append(ca)
                    print(f"  [{time.time()-started:.1f}s] canvas_action: {json.dumps(ca, ensure_ascii=False)[:240]}")
                elif t == "chart":
                    chart_events.append({"chart_type": ev.get("chart_type"), "has_option": bool(ev.get("option"))})
                elif t == "error":
                    errors.append(f"SSE_error: {ev.get('message','')[:200]}")

    print("\n=== 事件计数 ===")
    for k, v in sorted(events_by_type.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} x {v}")
    print("\n=== render_chart 工具结果明细 ===")
    if not render_chart_results:
        print("  (没有命中任何 render_chart 名称的 tool_result，说明 Agent 可能没调 render_chart，或者 tool_result 里 name 字段缺失)")
    for i, r in enumerate(render_chart_results, 1):
        print(f"  第{i}次 render_chart → ok={r['ok']} has_ca={r['has_canvas_action']} ca_keys={r['canvas_action_keys']} err={pretty_snippet(r['error'], 160)} top_keys={r['other_keys_preview']}")
    print(f"\n=== canvas_action 总数：{len(canvas_actions)}")
    if not canvas_actions:
        print("  ❌ 完全没有 canvas_action —— 前端 onCanvasAction 收不到落块指令，画布上当然没有图表！")
    else:
        for i, ca in enumerate(canvas_actions, 1):
            print(f"  #{i}: {json.dumps(ca, ensure_ascii=False)[:240]}")
    if chart_events:
        print(f"\n=== chart 事件（只用于消息提示，不会画布落块）：{len(chart_events)}")
        for c in chart_events:
            print(f"  type={c['chart_type']} has_option={c['has_option']}")
    if errors:
        print(f"\n=== errors ({len(errors)})")
        for e in errors[:5]:
            print(f"  ! {e}")
    print(f"\n=== session_id = {session_id}")
    # 简单断言
    rc = len(render_chart_results)
    ca = len(canvas_actions)
    if rc and ca == 0:
        print("\n!! 根因锁定：render_chart 工具执行了但是没有回 canvas_action → 画布无法落图表")
    elif rc and rc > ca:
        print(f"\n!! 部分根因：{rc} 次 render_chart 只产生了 {ca} 个 canvas_action → 部分图表没落到画布")
    elif rc and rc <= ca and ca:
        print("\n[OK] 后端 canvas_action 产出正常，如果仍然没图，bug 在前端 FreeCanvas.onCanvasAction 接收/落块那一侧")
    else:
        print("\n? 信息不足：本次运行没有触发任何 render_chart，换更明确的问题再试")


def main():
    token = login()
    ds, fields = pick_ds(token)
    print("DS:", ds.get("name"), "fields:", fields[:20])
    run(token, ds, fields)


if __name__ == "__main__":
    main()
