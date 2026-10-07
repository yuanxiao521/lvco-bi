# -*- coding: utf-8 -*-
"""记忆回流真机端到端探针（只读 + 新建一个测试会话，不碰既有数据）。

验证目标：
  R1..R4  第 4 轮触发首次累积合并（covered_rounds = U-1 = 3），summary 含埋点口径
  R7      再攒 4 轮后第二次合并，covered_rounds = 6，且**仍含**首次埋的口径（保旧纳新）
  R8      追问口径 → 应能答对（依赖长期记忆回流）

用法（必须让 app 包解析到 backend/，故在 backend 目录下执行）：
  ./.venv/Scripts/python.exe scripts/_probe_memory_e2e.py --email test@lvco.bi
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000/api/v1"

ROUNDS: list[str] = [
    "你好",
    "记住：我的口径是全公司含税，后面所有销售额都按这个口径算。",
    "客单价一般是按什么算的？",
    "好，先这样。",
    "顺便问一句，看趋势一般关注哪些维度？",
    "知道了。",
    "那我们继续聊。",
    "我的口径是什么？",
]

KEY_EVENTS = {"memory_saved", "compressed_history", "done", "error", "status"}


async def run_round(client: httpx.AsyncClient, token: str, sid: str, idx: int, msg: str) -> dict:
    """跑一轮对话，返回本轮收集到的关键事件（含 memory 载荷）。"""
    seen: dict = {"idx": idx, "msg": msg, "text": "", "events": []}
    payload = {"sessionId": sid, "message": msg}
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    async with client.stream("POST", f"{BASE}/ai/chat/stream", json=payload,
                             headers=headers, timeout=300.0) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {resp.status_code}: {body}")
        # 后端 SSE 为单行 `data: {json}`（事件类型在 JSON 的 type 字段里，无 event: 行）
        data_lines: list[str] = []
        async for raw in resp.aiter_lines():
            line = raw.rstrip("\r")
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
                continue
            if line != "" or not data_lines:
                continue
            # 空行 = 一条事件结束
            try:
                data = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                data_lines = []
                continue
            data_lines = []
            if not isinstance(data, dict):
                continue
            etype = data.get("type", "")
            if etype in KEY_EVENTS:
                seen["events"].append({"type": etype, "data": data})
            if etype == "message":
                seen["text"] += str(data.get("delta", ""))
            if etype == "done":
                break
    return seen


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="test@lvco.bi")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    global BASE
    BASE = f"{args.base_url}/api/v1"

    # 用应用自身的 settings 直接签令牌（避免依赖账号密码）
    from sqlalchemy import select

    from app.core.database import async_session_factory
    from app.core.security import create_access_token
    from app.models.user import User

    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == args.email))).scalar_one_or_none()
    if user is None:
        print(f"[FATAL] 找不到用户 {args.email}")
        return 2
    token = create_access_token(str(user.id))
    print(f"[INFO] user={args.email} id={user.id}")

    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE}/ai/sessions", json={"title": "记忆回流E2E探针"},
                              headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
        r.raise_for_status()
        sid = r.json()["data"]["id"]
        print(f"[INFO] session_id={sid}\n")

        for i, msg in enumerate(ROUNDS, 1):
            try:
                out = await run_round(client, token, sid, i, msg)
            except Exception as e:  # noqa: BLE001
                print(f"R{i} | 失败: {e}")
                return 1
            mem = [e for e in out["events"] if e["type"] in ("memory_saved", "compressed_history")]
            err = [e for e in out["events"] if e["type"] == "error"]
            tail = out["text"].strip().replace("\n", " ")[:80]
            flag = ""
            if mem:
                ev = mem[-1]["data"]
                flag = f"  <<< MEMORY covered={ev.get('covered_rounds')} len={len(ev.get('summary') or '')}"
            if err:
                flag += f"  <<< ERROR {err[0]['data']}"
            print(f"R{i} | 问: {msg[:28]:<30} | 答: {tail}{flag}")
            for e in mem:
                if e["type"] == "compressed_history":
                    print(f"      summary = {e['data'].get('summary')}")

        # 收尾：直接查库核对长期记忆实际落库结果
        from sqlalchemy import select

        from app.core.database import async_session_factory
        from app.models.ai_memory import AIMemory

        async with async_session_factory() as db:
            row = (await db.execute(
                select(AIMemory).where(AIMemory.session_id == uuid.UUID(sid))
            )).scalar_one_or_none()
        print("\n[DB] ai_memories:")
        if row is None:
            print("  （无行 —— 记忆未落库）")
        else:
            print(f"  covered_rounds = {row.covered_rounds}")
            print(f"  summary        = {row.summary}")
            ok = "全公司含税" in (row.summary or "")
            print(f"  埋点口径在摘要中: {'是' if ok else '否'}")

    print(f"[SESSION] {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
