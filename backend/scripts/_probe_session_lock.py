"""会话级并发锁探针：验证「同一会话的第二个并发请求被拒 + 本轮结束后锁已释放」。

需要后端已重启到含锁版本（app/services/session_lock.py）。

用法：
    python scripts/_probe_session_lock.py
    python scripts/_probe_session_lock.py --base http://127.0.0.1:8000/api/v1 --email test@lvco.bi --password 123456

判定：
  阶段 1（确定性）：外部往 Redis 预置 `lvco:ai:session:lock:{sid}` → 请求必须被拒且不进 Agent；清掉后可正常跑
  阶段 2（真并发）：同一会话两个请求错开 0.3s 发出 → 恰好一个被拒、另一个正常跑完（并校验两请求确实是同一会话）
  阶段 3：并发结束后 Redis 无残留锁，且可再次正常进入（证明是 finally 释放，不是 TTL 兜底）
"""
from __future__ import annotations

import argparse
import json
import threading
import time

import requests

REQUEST_TIMEOUT = 30
SSE_TIMEOUT = 300
BUSY_MARK = "上一轮还在处理中"


def _login(base: str, email: str, password: str) -> str:
    resp = requests.post(
        f"{base}/auth/login",
        json={"email": email, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(f"[环境] 登录失败 {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("data", {}).get("accessToken") or ""
    if not token:
        raise SystemExit("[环境] 登录响应里没有 accessToken")
    return str(token)


def _create_session(base: str, token: str) -> str:
    resp = requests.post(
        f"{base}/ai/sessions",
        json={},
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SystemExit(f"[环境] 建会话失败 {resp.status_code}: {resp.text[:200]}")
    data = resp.json().get("data", {})
    return str(data.get("id"))


def _create_canvas(base: str, token: str, title: str) -> str:
    resp = requests.post(
        f"{base}/canvases",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SystemExit(f"[环境] 建画布失败 {resp.status_code}: {resp.text[:200]}")
    return str(resp.json().get("data", {}).get("id"))


CANVAS_TITLE = "并发锁验证画布"
# 探针所有消息都带这个标记：会话标题取消息前 30 字，--cleanup 据此精确回收探针造的数据
PROBE_MARKER = "[锁探针] "
# 加标记之前的历史探针消息（旧残留清理用，按会话标题精确匹配）
LEGACY_TITLES = {
    "你好，一句话回答就行。",
    "你好，一句话回答。",
    "这一条应该被并发锁拒掉。",
    "这条应该被并发锁拒掉。",
    "锁已清掉，这条应该正常。",
    "再发一条，确认锁已释放。",
    "第三次，确认锁已释放。",
    "并发测试：第一条。",
    "并发测试：第二条。",
}


def _delete_canvas(base: str, token: str, canvas_id: str) -> bool:
    resp = requests.delete(
        f"{base}/canvases/{canvas_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    return resp.status_code == 200


def _cleanup_probe_canvases(base: str, token: str) -> int:
    """删掉本探针历史上建过的验证画布（按标题匹配），避免污染画布列表。"""
    resp = requests.get(
        f"{base}/canvases",
        params={"page": 1, "page_size": 100},
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return 0
    data = resp.json().get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    n = 0
    for it in items or []:
        if isinstance(it, dict) and it.get("title") == CANVAS_TITLE:
            n += _delete_canvas(base, token, str(it.get("id")))
    return n


def _list_sessions(base: str, token: str) -> list[dict]:
    resp = requests.get(
        f"{base}/ai/sessions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    data = resp.json().get("data")
    return data if isinstance(data, list) else []


def _cleanup_probe_sessions(base: str, token: str) -> int:
    """删掉探针建的会话（带标记的 + 标记之前的历史消息标题），避免污染会话列表。"""
    n = 0
    for s in _list_sessions(base, token):
        title = str(s.get("title") or "")
        if not (title.startswith(PROBE_MARKER) or title in LEGACY_TITLES):
            continue
        resp = requests.delete(
            f"{base}/ai/sessions/{s.get('id')}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
        n += 1 if resp.status_code == 200 else 0
    return n


_ALL_STREAMS: list[tuple[str, list[dict]]] = []


def _post_sse(url: str, token: str, payload: dict) -> list[dict]:
    """发一条消息流式收 SSE 事件（同步，跑完才返回）。"""
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
        stream=True,
        timeout=SSE_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(f"[环境] {url} 失败 {resp.status_code}: {resp.text[:200]}")
    events: list[dict] = []
    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                events.append(json.loads(raw[len("data:"):].strip()))
            except json.JSONDecodeError:
                continue
    except requests.exceptions.ChunkedEncodingError as exc:
        # 服务端收尾前断开（典型：generator 关闭时抛异常）→ 记显式标记，交给检查项判负
        events.append({"type": "stream_broken", "message": str(exc)})
    _ALL_STREAMS.append((url, events))
    return events


def _stream(base: str, token: str, message: str, session_id: str) -> list[dict]:
    return _post_sse(
        f"{base}/ai/chat/stream", token,
        {"message": PROBE_MARKER + message, "sessionId": session_id},
    )


def _stream_canvas(base: str, token: str, message: str, canvas_id: str) -> list[dict]:
    # 不传 sessionId：走前端真实路径（服务端按 canvasId 解析出该画布的会话）
    return _post_sse(
        f"{base}/ai/canvas/chat", token,
        {"message": PROBE_MARKER + message, "canvasId": canvas_id},
    )


def _types(events: list[dict]) -> str:
    counts: dict[str, int] = {}
    for ev in events:
        t = str(ev.get("type"))
        counts[t] = counts.get(t, 0) + 1
    return ", ".join(f"{k}×{v}" for k, v in counts.items()) or "<无事件>"


def _busy_error(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") == "error" and BUSY_MARK in str(ev.get("message", "")):
            return str(ev.get("message"))
    return None


def _sid(events: list[dict]) -> str:
    """从 session_created 事件里取会话 id（确认三个请求用的是同一个会话）。"""
    for ev in events:
        if ev.get("type") == "session_created":
            sess = ev.get("session") if isinstance(ev.get("session"), dict) else {}
            return str(sess.get("id") or ev.get("session_id") or "")
    return ""


def _has_agent_work(events: list[dict]) -> bool:
    return any(e.get("type") in ("message", "tool_call", "canvas_action", "report") for e in events)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--email", default="test@lvco.bi")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--cleanup", action="store_true",
                        help="只清理探针造的数据（验证画布 + 会话）后退出，不跑测试、不耗 token")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    if args.cleanup:
        token = _login(base, args.email, args.password)
        n_canvas = _cleanup_probe_canvases(base, token)
        n_session = _cleanup_probe_sessions(base, token)
        print(f"[清理] 探针画布 {n_canvas} 个、探针会话 {n_session} 个已删除")
        return 0

    try:
        from redis import Redis
        rds = Redis.from_url(args.redis_url, decode_responses=True)
        rds.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"[跳过] Redis 不可用（{exc}）→ 锁会走内存降级，无法从外部预置锁；"
              f"只跑真并发阶段")
        rds = None

    token = _login(base, args.email, args.password)
    checks: list[tuple[str, bool]] = []

    # ── 阶段 1（确定性）：外部预置锁 → 请求必须被拒，且不进 Agent ──────────────
    s1 = _create_session(base, token)
    if rds is not None:
        key = f"lvco:ai:session:lock:{s1}"
        rds.set(key, "preset-by-probe", ex=120)
        e1 = _stream(base, token, "这条应该被并发锁拒掉。", s1)
        busy1 = _busy_error(e1)
        print(f"[1] 预置锁 → {_types(e1)}  busy={busy1}")
        rds.delete(key)
        e1b = _stream(base, token, "锁已清掉，这条应该正常。", s1)
        print(f"[1] 清除锁 → {_types(e1b)}  busy={_busy_error(e1b)}")
        checks += [
            ("预置锁时被拒", busy1 is not None),
            ("被拒时未进 Agent（无 message/tool）", not _has_agent_work(e1)),
            ("被拒时有 done 收尾", any(e.get("type") == "done" for e in e1)),
            ("清除锁后可正常跑", _busy_error(e1b) is None and _has_agent_work(e1b)),
        ]

    # ── 阶段 2（真并发）：同一会话两个请求错开 0.3s 发出，恰好一个被拒 ────────
    s2 = _create_session(base, token)
    print(f"\n[env] 并发阶段 session={s2}")
    res_a: list[dict] = []
    res_b: list[dict] = []
    done_a = threading.Event()
    done_b = threading.Event()

    def _run(store: list[dict], flag: threading.Event, msg: str) -> None:
        try:
            store.extend(_stream(base, token, msg, s2))
        finally:
            flag.set()

    threading.Thread(target=_run, args=(res_a, done_a, "并发测试：第一条。"), daemon=True).start()
    time.sleep(0.3)
    threading.Thread(target=_run, args=(res_b, done_b, "并发测试：第二条。"), daemon=True).start()
    done_a.wait(timeout=SSE_TIMEOUT)
    done_b.wait(timeout=SSE_TIMEOUT)

    busy_a, busy_b = _busy_error(res_a), _busy_error(res_b)
    print(f"[2] A（先发）{_types(res_a)}  busy={busy_a}")
    print(f"[2] B（+0.3s）{_types(res_b)}  busy={busy_b}")
    print(f"[2] 会话归属 A={_sid(res_a)} B={_sid(res_b)} 期望={s2}")
    refused = [bool(busy_a), bool(busy_b)]
    checks += [
        # 被拒的请求在落库前就 return，因此不会有 session_created（属预期，不是异常）
        ("放行的请求落在指定会话上", _sid(res_a if not busy_a else res_b) == s2),
        ("被拒的请求没有新建会话（未落任何消息）", _sid(res_a if busy_a else res_b) in ("", s2)),
        ("恰好一个被拒（互斥）", refused.count(True) == 1),
        ("被拒的那个没进 Agent", not _has_agent_work(res_a if busy_a else res_b)),
        ("放行的那个正常跑完", _has_agent_work(res_b if busy_a else res_a)),
    ]

    # ── 阶段 3：并发结束后锁已释放（不靠 TTL 兜底） ────────────────────────────
    e3 = _stream(base, token, "再发一条，确认锁已释放。", s2)
    print(f"[3] {_types(e3)}  busy={_busy_error(e3)}")
    if rds is not None:
        left = rds.get(f"lvco:ai:session:lock:{s2}")
        print(f"[3] Redis 残留锁 = {left!r}")
        checks.append(("结束后 Redis 无残留锁", left is None))
    checks.append(("结束后可再次进入", _busy_error(e3) is None))

    # ── 阶段 4：画布入口同样受保护（会话 id 由服务端按 canvasId 解析） ─────────
    if rds is not None:
        canvas_id = _create_canvas(base, token, CANVAS_TITLE)
        first = _stream_canvas(base, token, "你好，一句话回答。", canvas_id)
        csid = _sid(first)
        print(f"\n[4] 画布 canvas={canvas_id} session={csid}  {_types(first)}")
        if not csid:
            checks.append(("画布入口：拿到服务端解析的会话 id", False))
        else:
            rds.set(f"lvco:ai:session:lock:{csid}", "preset-by-probe", ex=120)
            second = _stream_canvas(base, token, "这条应该被并发锁拒掉。", canvas_id)
            busy4 = _busy_error(second)
            print(f"[4] 预置锁 → {_types(second)}  busy={busy4}")
            rds.delete(f"lvco:ai:session:lock:{csid}")
            checks += [
                ("画布入口：预置锁时被拒", busy4 is not None),
                ("画布入口：被拒时无落块动作", not any(
                    e.get("type") in ("canvas_action", "message") for e in second
                )),
                ("画布入口：被拒时有 done 收尾", any(e.get("type") == "done" for e in second)),
            ]

    print()
    broken = [u for u, evs in _ALL_STREAMS if any(ev.get("type") == "stream_broken" for ev in evs)]
    checks.append(("所有流都正常收尾（无断流）", not broken))
    if broken:
        print(f"[断流] {broken}")
    ok = True
    for name, passed in checks:
        ok = ok and passed
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"\n{'全部通过：会话级并发锁生效' if ok else '存在失败项，见上'}")

    # 清理探针造的数据：验证画布（软删）+ 会话（硬删），避免污染画布/会话列表
    n_canvas = _cleanup_probe_canvases(base, token)
    n_session = _cleanup_probe_sessions(base, token)
    print(f"[清理] 探针画布 {n_canvas} 个、探针会话 {n_session} 个已删除")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())