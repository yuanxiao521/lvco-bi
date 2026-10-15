# -*- coding: utf-8 -*-
"""真机验证：画布入口任务态是否只写 Redis（画布改造 A 的验收）。

验证项（打真机 :8000 + 真 Redis）：
  1. 执行期间：Redis 直查 ai:canvas:task:{sid} 存在且 status=running
  2. 执行期间：GET /ai/canvases/{cid}/sessions 的 interrupted=False（不得误报未完成）
  3. 正常完成后：Redis 键消失——用**独立 Redis 连接**直查。
     为什么必须独立直查：应用进程内的 get 带"内存副本回退"，即便 Redis 里已经没了
     也可能读回旧值，证明不了"Redis 里真的清了"。外部连接才是权威。
  4. 端到端无回归：画布对话仍能跑完，assistant 消息已落库。

用法：.venv\\Scripts\\python.exe scripts/_verify_canvas_task_state.py
前置：后端已起（:8000）、Redis 可用、LLM 网关可用
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 控制台可能是 GBK（中文 Windows）：报告里有中文与符号，统一按 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import redis  # noqa: E402
import requests  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.database import async_session_factory  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.models.user import User  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
DS = "970d798d-f5da-4442-8d25-3563020d5a29"
NO_PROXY = {"proxies": {"http": None, "https": None}}

st = {"sid": None, "done": False, "error": None, "saw_error_event": False, "events": []}


def make_token() -> str:
    async def _q():
        async with async_session_factory() as db:
            u = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one()
            return str(u.id)
    return create_access_token(asyncio.run(_q()))


def run_chat(h: dict, cid: str) -> None:
    payload = {
        "datasource_id": DS,
        "session_id": None,
        "message": "查询各地区销售额，用一句话总结",
        "canvas_id": cid,
        "new_session": True,
    }
    try:
        with requests.post(
            f"{BASE}/ai/canvas/chat",
            headers={**h, "Accept": "text/event-stream", "Content-Type": "application/json"},
            json=payload, stream=True, **NO_PROXY, timeout=(10, 300),
        ) as resp:
            if resp.status_code != 200:
                st["error"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
                return
            buf = ""
            for raw in resp.iter_content(chunk_size=1024, decode_unicode=True):
                if not raw:
                    continue
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
                    except Exception:
                        continue
                    t = ev.get("type")
                    st["events"].append(t)
                    if t == "session_created":
                        st["sid"] = ev.get("session_id") or (ev.get("session") or {}).get("id")
                    if t == "error":
                        st["saw_error_event"] = True
                    if t == "done":
                        st["done"] = True
    except Exception as e:  # noqa: BLE001
        st["error"] = repr(e)


def main() -> int:
    token = make_token()
    h = {"Authorization": f"Bearer {token}"}

    rc = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    print(f"Redis = {settings.redis_url}  ping={rc.ping()}")

    r = requests.post(f"{BASE}/canvases", headers=h, json={"title": "画布任务态验证", "datasource_id": DS}, **NO_PROXY, timeout=20)
    assert r.status_code in (200, 201), r.text[:300]
    cid = r.json()["data"]["id"]
    print(f"画布 = {cid[:8]}")

    th = threading.Thread(target=run_chat, args=(h, cid), daemon=True)
    th.start()

    t0 = time.time()
    while st["sid"] is None and time.time() - t0 < 40:
        time.sleep(0.2)
    sid = st["sid"]
    if not sid:
        print(f"❌ 未拿到 session_id  error={st['error']}  events={st['events'][:8]}")
        return 1
    print(f"会话 = {sid[:8]}")

    key = f"lvco:ai:canvas:task:{sid}"
    seen_running = False
    interrupted_during = "N/A"
    statuses = []
    while not st["done"] and time.time() - t0 < 300:
        v = rc.get(key)
        if v:
            s = json.loads(v).get("status")
            if s not in statuses:
                statuses.append(s)
            if s == "running":
                seen_running = True
                if interrupted_during == "N/A":
                    rr = requests.get(f"{BASE}/ai/canvases/{cid}/sessions", headers=h, **NO_PROXY, timeout=15)
                    for it in rr.json()["data"]:
                        if it["id"] == sid:
                            interrupted_during = it.get("interrupted")
        time.sleep(0.3)
    th.join(timeout=60)

    print(f"事件类型 = {sorted(set(st['events']))}")
    print(f"执行期间 Redis 状态序列 = {statuses}  |  出现过 running = {seen_running}")
    print(f"执行期间 interrupted = {interrupted_during}")

    # 完成后：Redis 键应消失（外部直查）
    gone = None
    t1 = time.time()
    while time.time() - t1 < 15:
        v = rc.get(key)
        if v is None:
            gone = round(time.time() - t1, 1)
            break
        time.sleep(0.2)
    tail = None if gone is not None else rc.get(key)
    print(f"完成后 Redis 键清除耗时 = {gone}s" + (f"  |  仍存在 = {tail}" if gone is None else ""))

    # assistant 落库
    rm = requests.get(f"{BASE}/ai/sessions/{sid}/messages", headers=h, **NO_PROXY, timeout=20)
    items = rm.json().get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    roles = [m.get("role") for m in items]
    print(f"消息角色 = {roles}")

    # 判定
    normal = st["done"] and not st["saw_error_event"]
    checks = {
        "执行期间写入 running（真 Redis 可见）": seen_running,
        "执行期间不误报 interrupted": interrupted_during is False,
        "正常完成后 Redis 键被清除（外部直查）": gone is not None,
        "assistant 消息已落库": "assistant" in roles,
        "端到端正常结束（无 error 事件）": bool(normal),
    }
    print()
    for k, v in checks.items():
        print(f"  [{'OK' if v else 'NG'}] {k}")
    ok = all(checks.values())
    if not ok and not normal:
        print("\n注：任务本身未正常结束（LLM 网关/上游异常），请先排除环境因素再判定。")
    print("\n结论:", "✅ 画布改造 A 真机验证通过" if ok else "❌ 未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
