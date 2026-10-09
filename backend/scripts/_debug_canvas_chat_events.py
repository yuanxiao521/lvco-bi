"""复现 canvas/chat 事件流并定位"卡住"：带时间戳、空闲间隔告警、done 缺失检测。

用法：
    python scripts/_debug_canvas_chat_events.py                 # 自动挑块最多的画布
    python scripts/_debug_canvas_chat_events.py --canvas <uuid> # 指定画布
    python scripts/_debug_canvas_chat_events.py --message "..." # 指定问题

关注点：
- 每个事件打印 [耗时] 类型，间隔超过 GAP_WARN 秒会打 !!GAP
- 流结束（或超时断开）时汇总：是否收到 done、最长静默多久、停在哪个事件之后
需要 test@lvco.bi / 123456。
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import requests

BASE = "http://127.0.0.1:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL = "test@lvco.bi"
PASSWORD = "123456"
DEFAULT_MESSAGE = "为什么这个图表没有数据，是空白的？"
GAP_WARN = 8.0      # 事件间隔超过它就打告警（正常每步 ≤45s，但应该有进度事件）
STALL_STOP = 150.0  # 这么久没有任何字节就判定卡死并断开（后端看门狗 90s 应先触发）


def login() -> str:
    r = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD},
                      **NO_PROXY, timeout=15)
    print("login status", r.status_code)
    body = r.json()
    if r.status_code != 200:
        print("login failed:", body)
        sys.exit(1)
    return body["data"]["accessToken"] if "data" in body else body["access_token"]


def pick_canvas(token: str, canvas_id: str | None) -> dict | None:
    """挑一个"有块"的画布（最贴近用户场景）；没有则返回 None（草稿）。"""
    r = requests.get(f"{BASE}/canvases", headers={"Authorization": f"Bearer {token}"},
                     **NO_PROXY, timeout=15)
    body = r.json()
    items = body.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    print(f"canvases: {len(items)} 个")
    if canvas_id:
        picked = next((c for c in items if str(c.get("id")) == canvas_id), None)
    else:
        with_blocks = [c for c in items if (c.get("blocks") or [])]
        with_blocks.sort(key=lambda c: -len(c.get("blocks") or []))
        picked = with_blocks[0] if with_blocks else None
    if picked:
        blocks = picked.get("blocks") or []
        kinds = {}
        for b in blocks:
            k = b.get("type") or "?"
            kinds[k] = kinds.get(k, 0) + 1
        print(f"pick canvas: {picked.get('id')} 「{picked.get('title')}」 blocks={len(blocks)} {kinds}")
    else:
        print("no canvas with blocks → 以草稿(无 canvas_id)方式发送")
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canvas", default=None)
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--datasource", default=None)
    ap.add_argument("--base", default=None, help="覆盖后端地址，如 http://127.0.0.1:8001/api/v1")
    args = ap.parse_args()

    global BASE
    if args.base:
        BASE = args.base.rstrip("/")
        print("base:", BASE)

    token = login()
    canvas = pick_canvas(token, args.canvas)

    ds_id = args.datasource or (canvas or {}).get("datasourceId")
    if not ds_id:
        r = requests.get(f"{BASE}/datasources?page=1&page_size=5",
                         headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
        body = r.json()
        items = (body.get("data") or {}).get("items") if isinstance(body.get("data"), dict) else body.get("data")
        items = items or []
        if not items:
            print("no datasource:", body)
            sys.exit(1)
        ds_id = items[0]["id"]
        print("pick ds:", ds_id, items[0].get("name"))
        fields = ((items[0].get("schemaMeta") or {}).get("fields")
                  or (items[0].get("schema_meta") or {}).get("fields") or [])
    else:
        fields = []
        r = requests.get(f"{BASE}/datasources/{ds_id}",
                         headers={"Authorization": f"Bearer {token}"}, **NO_PROXY, timeout=15)
        if r.status_code == 200:
            d = (r.json().get("data") or {})
            fields = ((d.get("schemaMeta") or {}).get("fields")
                      or (d.get("schema_meta") or {}).get("fields") or [])
        print("ds from canvas:", ds_id, f"fields={len(fields)}")

    payload = {
        "datasource_id": ds_id,
        "canvas_id": (canvas or {}).get("id"),
        "session_id": None,
        "message": args.message,
        "canvas_context": {"availableFields": fields},
    }

    print("=== canvas/chat stream ===")
    types: dict[str, int] = {}
    text_total = 0
    started = time.time()
    last_evt_at = started
    last_gap = 0.0
    last_type = "<start>"
    gaps: list[tuple[float, str, str]] = []   # (gap秒, 前置事件, 后置事件)
    got_done = False
    got_error = ""
    stalled = False

    with requests.post(
        f"{BASE}/ai/canvas/chat",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        json=payload, stream=True, **NO_PROXY, timeout=(10, STALL_STOP),
    ) as r:
        print("HTTP", r.status_code)
        if r.status_code != 200:
            print(r.text[:1500])
            return
        buf = ""
        try:
            for raw in r.iter_content(chunk_size=1024, decode_unicode=True):
                now = time.time()
                if not raw:
                    continue
                gap = now - last_evt_at
                if gap >= GAP_WARN:
                    print(f"\n!!GAP {gap:.1f}s 静默（上一事件: {last_type}）", flush=True)
                buf += raw
                while "\n" in buf:
                    ln, buf = buf.split("\n", 1)
                    if not ln.startswith("data: "):
                        continue
                    js = ln[6:].strip()
                    if not js:
                        continue
                    try:
                        ev = json.loads(js)
                    except Exception as e:  # noqa: BLE001
                        print("BADLINE", js[:120], "err", e)
                        continue
                    t = ev.get("type") or "?"
                    types[t] = types.get(t, 0) + 1
                    el = time.time() - started
                    if t == "message":
                        text_total += len(ev.get("delta", ""))
                        print(".", end="", flush=True)
                    elif t == "progress":
                        print(f"\n[{el:.1f}s] progress {ev.get('index')}/{ev.get('total')} "
                              f"{ev.get('title')} {ev.get('status')}", flush=True)
                        if gap >= GAP_WARN:
                            gaps.append((gap, last_type, t))
                    elif t in ("tool_call", "tool_result", "done", "error", "canvas_action",
                               "decision", "intent", "plan", "status"):
                        extra = ""
                        if t == "tool_call":
                            extra = f" name={ev.get('name')} args={list((ev.get('args') or {}).keys())}"
                        elif t == "tool_result":
                            r0 = ev.get("result", "") or ""
                            try:
                                r0 = json.loads(r0)
                            except Exception:  # noqa: BLE001
                                pass
                            extra = f" name={ev.get('name')} <- {str(r0)[:160]}"
                        elif t == "decision":
                            extra = f" action={ev.get('action')} tool={ev.get('tool')}"
                        elif t == "error":
                            extra = f" {ev.get('message')}"
                            got_error = str(ev.get("message"))
                        if t == "done":
                            got_done = True
                        print(f"\n[{el:.1f}s] {t}{extra}", flush=True)
                        if gap >= GAP_WARN and t != "done":
                            gaps.append((gap, last_type, t))
                    else:
                        print(f"\n[{el:.1f}s] {t} {json.dumps(ev, ensure_ascii=False)[:140]}", flush=True)
                    last_evt_at = time.time()
                    last_gap = max(last_gap, gap)
                    last_type = t
        except requests.exceptions.ReadTimeout:
            stalled = True
            print(f"\n!!! 客户端断开：{STALL_STOP:.0f}s 内没有任何字节（后端看门狗应在 90s 先收尾）")
        except Exception as e:  # noqa: BLE001
            print(f"\n!!! 读取异常: {type(e).__name__}: {e}")

    total = time.time() - started
    print(f"\n=== 汇总（{total:.1f}s）===")
    for k, v in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {k:20s} x {v}")
    print(f"  message 累计字符: {text_total}")
    print(f"  done 收到: {got_done}    error: {got_error or '（无）'}   客户端强制断开: {stalled}")
    print(f"  最长静默: {last_gap:.1f}s    最后一个事件: {last_type}")
    if gaps:
        print("  静默间隔明细：")
        for g, a, b in gaps[:10]:
            print(f"    {g:.1f}s  {a} → {b}")
    if not got_done:
        print("!! 流未以 done 结束 → 前端会一直停在『思考中』（除非客户端/看门狗断开）")
    else:
        print("OK：流正常以 done 结束")


if __name__ == "__main__":
    main()