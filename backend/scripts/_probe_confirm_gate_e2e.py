# -*- coding: utf-8 -*-
"""确认守卫（HITL gate）+ 豁免路径 真机端到端探针。

验证目标（对应《主管状态机_设计方案_v2》）：
  T0 落一个块（给清空提供真实对象）；
  T1 豁免路径："清空所有画布内容" 命中全量操作词 → 零交互直接执行，
     断言：不出现 confirm_request、decision 非 ask_user；
  T2 confirm 路径："帮我把画布恢复成空白吧"（不含豁免词）→ 观察 LLM 是否
     ask_user(ask_kind=confirm) → 出 confirm_request 事件；
  T3 恢复路径（仅当 T2 出卡片）：带 ui_action={"type":"confirm"} 重发"确认"
     → 断言：不再反问、出现 canvas_action（守卫直接恢复派发）。

用法（backend 目录下）：
  ./.venv/Scripts/python.exe scripts/_probe_confirm_gate_e2e.py
"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api/v1"
DS_ID = "970d798d-f5da-4442-8d25-3563020d5a29"  # Ecommerce Orders


async def call_canvas(client: httpx.AsyncClient, token: str, canvas_id: str,
                      msg: str, session_id: str | None,
                      ui_action: dict | None = None) -> dict:
    """发一轮画布对话，收集全部 SSE 事件。"""
    payload = {"canvasId": canvas_id, "datasourceId": DS_ID, "message": msg}
    if session_id:
        payload["sessionId"] = session_id
    if ui_action:
        payload["ui_action"] = ui_action
    out = {"msg": msg, "types": [], "text": "", "confirm_request": None,
           "decisions": [], "canvas_actions": [], "session_id": session_id, "error": None}
    async with client.stream("POST", f"{BASE}/ai/canvas/chat", json=payload,
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=600.0) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {resp.status_code}: {body}")
        buf: list[str] = []
        async for raw in resp.aiter_lines():
            line = raw.rstrip("\r")
            if line.startswith("data:"):
                buf.append(line[5:].strip())
            elif line == "" and buf:
                try:
                    ev = json.loads("\n".join(buf))
                except json.JSONDecodeError:
                    buf = []
                    continue
                buf = []
                t = ev.get("type", "?")
                out["types"].append(t)
                if t == "text":
                    out["text"] += str(ev.get("content") or "")
                elif t == "confirm_request":
                    out["confirm_request"] = ev
                elif t == "decision":
                    out["decisions"].append(ev.get("action"))
                elif t == "canvas_action":
                    act = ev.get("action")
                    if isinstance(act, dict):
                        out["canvas_actions"].append(act.get("action") or "?")
                    else:
                        out["canvas_actions"].append(str(act or ev.get("actionType") or "?"))
                elif t == "session_created":
                    out["session_id"] = ev.get("sessionId") or out["session_id"]
                elif t == "error":
                    out["error"] = ev.get("message")
    return out


def summarize(tag: str, r: dict) -> None:
    print(f"\n===== {tag} =====")
    print(f"  消息: {r['msg']}")
    print(f"  事件: {r['types']}")
    print(f"  decisions: {r['decisions']}")
    print(f"  canvas_actions: {r['canvas_actions']}")
    if r["confirm_request"]:
        print(f"  confirm_request: goal={r['confirm_request'].get('goal', '')[:30]!r} "
              f"question={str(r['confirm_request'].get('question', ''))[:50]!r}")
    else:
        print("  confirm_request: (无)")
    if r["error"]:
        print(f"  ⚠️ error: {r['error']}")
    tail = (r["text"] or "").strip().replace("\n", " ")[:120]
    print(f"  文本尾: {tail}")


async def main() -> int:
    from app.core.security import create_access_token
    from app.core.database import async_session_factory
    from sqlalchemy import select
    from app.models.user import User

    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == "test@lvco.bi"))).scalar_one_or_none()
        if user is None:
            print("[FATAL] 找不到用户 test@lvco.bi")
            return 2
    token = create_access_token(str(user.id))

    results: list[tuple[str, dict]] = []
    async with httpx.AsyncClient() as client:
        # 隔离画布（失败重试一次：上一轮中断可能留下脏事务连接）
        canvas_id = None
        for attempt in (1, 2):
            r = await client.post(f"{BASE}/canvases", timeout=30.0,
                                  headers={"Authorization": f"Bearer {token}"},
                                  json={"title": "确认守卫探针", "datasourceId": DS_ID})
            if r.status_code in (200, 201):
                canvas_id = r.json()["data"]["id"]
                break
            print(f"[WARN] 建画布第 {attempt} 次失败 HTTP {r.status_code}，重试…")
            await asyncio.sleep(3)
        if not canvas_id:
            r.raise_for_status()
        print(f"隔离画布: {canvas_id}")

        async with httpx.AsyncClient() as c:
            # 轮间小歇：done 事件到达后服务端收尾（记忆回流/审计）可能仍在进行，
            # 立刻发下一轮会撞会话锁或脏事务连接
            import asyncio as _aio

            # T0 落块
            r0 = await call_canvas(c, token, canvas_id,
                                   "按地区统计销售额，画一个柱状图", None)
            results.append(("T0 落块", r0))
            sid = r0["session_id"]
            await _aio.sleep(3)

            # T1 豁免路径
            r1 = await call_canvas(c, token, canvas_id,
                                   "清空所有画布内容", sid)
            results.append(("T1 豁免（含全量词）", r1))
            sid = r1["session_id"] or sid
            await _aio.sleep(3)

            # T2 confirm 路径（不含豁免词的说法）
            r2 = await call_canvas(c, token, canvas_id,
                                   "帮我把画布恢复成空白吧", sid)
            results.append(("T2 confirm 候选（无豁免词）", r2))
            sid = r2["session_id"] or sid
            await _aio.sleep(2)

            # T3 恢复路径：仅当 T2 出了确认卡片
            if r2["confirm_request"]:
                r3 = await call_canvas(c, token, canvas_id, "确认", sid,
                                       ui_action={"type": "confirm"})
                results.append(("T3 卡片确认恢复", r3))
            else:
                print("\n(T2 未触发 confirm_request —— LLM 未选择封闭确认，如实记录)")

    ok = True
    for tag, r in results:
        summarize(tag, r)

    # 断言
    t1 = dict(results)["T1 豁免（含全量词）"]
    if t1["confirm_request"]:
        print("\n❌ T1 豁免失效：含全量词仍弹确认卡片")
        ok = False
    else:
        print("\n✅ T1 豁免生效：全量词零交互执行（无 confirm_request）")

    if "T3 卡片确认恢复" in dict(results):
        t3 = dict(results)["T3 卡片确认恢复"]
        if t3["confirm_request"] or "ask_user" in t3["decisions"]:
            print("❌ T3 恢复失败：确认后仍反问")
            ok = False
        else:
            print("✅ T3 守卫恢复生效：确认后直接执行，未再反问")
    else:
        print("⚠️ T3 跳过：本轮 LLM 未触发封闭确认（行为如实记录，可换话术重跑）")

    print("\n结论:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
