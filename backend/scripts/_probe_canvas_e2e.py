# -*- coding: utf-8 -*-
"""画布入口（/ai/canvas/chat）真机端到端探针。

覆盖对话入口测不到的三件事：
  1. 画布入口端到端可用（自建隔离画布，不动用户既有画布）；
  2. F4「画布历史过滤」在**真实 DB 行**上的行为（不是合成数据）：
     新规则保留有内容的 assistant、跳过空占位、按 id 排除本条用户消息；
     并量化"旧规则会丢多少条助手消息"；
  3. 画布会话的记忆回流（画布入口同样走 _save_memory → ai_memories）。

用法（在 backend 目录下）：
  ./.venv/Scripts/python.exe scripts/_probe_canvas_e2e.py --email test@lvco.bi
"""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DS_ID = "970d798d-f5da-4442-8d25-3563020d5a29"  # Ecommerce Orders (csv, 1200 行)
ROUNDS = [
    "这个数据源里有哪些字段？用一句话列出即可。",
    "好，先这样。",
    "再说一句：这份数据最适合做哪类分析？",
]

# --profile data：真取数的对话序列，用来验证"摘要在对话含数字时能否记下数字+口径+范围"
DATA_ROUNDS = [
    "各地区销售额分别是多少？请给出具体数字。",
    "哪个地区最高？差多少？",
    "客单价是多少？",
    "好，先这样。",
    "把刚才的关键数字汇总成一句结论。",
]
KEY_TYPES = {"session_created", "message", "compressed_history", "memory_saved",
             "error", "done", "canvas_action", "intent", "decision", "status"}


async def call_canvas(client: httpx.AsyncClient, base: str, token: str, canvas_id: str,
                      msg: str, session_id: str | None) -> dict:
    seen: dict = {"msg": msg, "text": "", "events": [], "canvas_actions": 0,
                  "session_id": session_id}
    payload = {"canvasId": canvas_id, "datasourceId": DS_ID, "message": msg}
    if session_id:
        payload["sessionId"] = session_id
    async with client.stream("POST", f"{base}/ai/canvas/chat", json=payload,
                             headers={"Authorization": f"Bearer {token}"}, timeout=600.0) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {resp.status_code}: {body}")
        buf: list[str] = []
        async for raw in resp.aiter_lines():
            line = raw.rstrip("\r")
            if line.startswith("data:"):
                buf.append(line[5:].strip())
                continue
            if line != "" or not buf:
                continue
            try:
                data = json.loads("\n".join(buf))
            except json.JSONDecodeError:
                buf = []
                continue
            buf = []
            if not isinstance(data, dict):
                continue
            t = data.get("type", "")
            if t == "session_created":
                seen["session_id"] = data.get("session_id") or (data.get("session") or {}).get("id")
            if t in KEY_TYPES:
                seen["events"].append(t)
            if t == "message":
                seen["text"] += str(data.get("delta", ""))
            if t == "canvas_action":
                seen["canvas_actions"] += 1
            if t == "done":
                break
    return seen


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="test@lvco.bi")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--profile", choices=["concept", "data"], default="concept",
                    help="concept=概念问答（不产生数字）；data=真取数对话（验证摘要能否记下数字）")
    args = ap.parse_args()
    rounds = DATA_ROUNDS if args.profile == "data" else ROUNDS
    base = f"{args.base_url}/api/v1"

    from sqlalchemy import select

    from app.api.v1.ai import _filter_canvas_history
    from app.core.database import async_session_factory
    from app.core.security import create_access_token
    from app.models.ai_memory import AIMemory
    from app.models.ai_message import AIMessage, AIMessageRole
    from app.models.user import User

    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == args.email))).scalar_one_or_none()
        if user is None:
            print(f"[FATAL] 找不到用户 {args.email}")
            return 2
    token = create_access_token(str(user.id))
    print(f"[INFO] user={args.email} id={user.id}\n")

    async with httpx.AsyncClient() as client:
        r = await client.post(f"{base}/canvases", timeout=30.0,
                              headers={"Authorization": f"Bearer {token}"},
                              json={"title": "E2E画布探针", "datasourceId": DS_ID})
        r.raise_for_status()
        canvas_id = r.json()["data"]["id"]
        print(f"[INFO] 新建隔离画布 canvas_id={canvas_id}（跑完可删）")

        sid = None
        for i, msg in enumerate(rounds, 1):
            try:
                out = await call_canvas(client, base, token, canvas_id, msg, sid)
            except Exception as e:  # noqa: BLE001
                print(f"R{i} | 失败: {e}")
                return 1
            sid = out["session_id"] or sid
            text = out["text"].strip().replace("\n", " ")[:70]
            print(f"R{i} | 问: {msg[:24]:<26} | 答: {text or '（无文本）'}")
            print(f"     事件: {out['events']}  落块: {out['canvas_actions']}")
        print(f"\n[SESSION] {sid}")

    # ── F4 验证：拿真实 DB 行跑 _filter_canvas_history ──
    # 注意：这里的查询必须与 ai.py 画布入口（order_by created_at.desc, role.desc）完全一致，
    # 否则测的是探针自己的排序而不是应用的行为。改动应用查询时这里要同步。
    async with async_session_factory() as db:
        rows = list(reversed((await db.execute(
            select(AIMessage).where(AIMessage.session_id == uuid.UUID(str(sid)))
            .order_by(AIMessage.created_at.desc(), AIMessage.role.desc()).limit(12)
        )).scalars().all()))
        cur_user = next((m for m in reversed(rows) if m.role == AIMessageRole.user), None)
        kept = _filter_canvas_history(rows, cur_user.id if cur_user else None)
        old_rule = [m for m in rows if m.role != AIMessageRole.assistant]  # 旧规则：跳过全部 assistant

        n_asst = sum(1 for m in rows if m.role == AIMessageRole.assistant)
        n_asst_empty = sum(1 for m in rows if m.role == AIMessageRole.assistant
                           and not (m.content or "").strip())
        n_asst_kept = sum(1 for m in kept if m["role"] == "assistant")
        cur_in_kept = sum(1 for m in kept if m["content"] == (cur_user.content if cur_user else None))

        print("\n[F4 真实数据校验] 取该会话最近 12 条消息")
        print(f"  消息总数={len(rows)}  助手消息={n_asst}（其中空占位={n_asst_empty}）")
        print(f"  新规则保留={len(kept)} 条（助手 {n_asst_kept} 条）")
        print(f"  旧规则只保留={len(old_rule)} 条 → F4 救回助手消息 {len(kept) - len(old_rule)} 条")
        print(f"  本条用户消息是否被误纳入: {'是 ✗' if cur_in_kept else '否 ✓'}")
        print("  新规则保留下来的角色序列: " + ",".join(
            f"{m['role']}{'(空)' if not (m['content'] or '').strip() else ''}" for m in kept))

        # 记忆合并输入那条路径：直接调用应用函数（不镜像查询，杜绝漂移）。
        # 判据说明：该函数会按设计跳过空内容消息，所以序列不保证 U/A 严格交替，
        # 正确的验证方式是"与规范序（asc+role）做等价比对"。
        from app.services.agents.lead.lead_agent import _load_unmerged_messages

        canon_rows = (await db.execute(
            select(AIMessage).where(AIMessage.session_id == uuid.UUID(str(sid)))
            .order_by(AIMessage.created_at.asc(), AIMessage.role.asc())
        )).scalars().all()
        canon = [(r.role.value if hasattr(r.role, "value") else str(r.role))
                 for r in canon_rows if str(r.content or "").strip()]

        merged, wm = await _load_unmerged_messages(db, str(sid), max_messages=24)
        actual = [str(m["role"]) for m in merged]
        # 判据：水位驱动下返回的是"水位之后**最早**一段"，不一定是全量尾部，
        # 所以正确的校验是"actual 必须是规范序（asc+role）的连续子序列"。
        n_ = len(actual)
        contained = (
            any(canon[i:i + n_] == actual for i in range(len(canon) - n_ + 1))
            if n_ else True
        )
        print(f"\n[记忆合并输入] _load_unmerged_messages → "
              f"{' '.join('U' if r == 'user' else 'A' for r in actual)}")
        print(f"               规范序（asc+role）全序列 → "
              f"{' '.join('U' if r == 'user' else 'A' for r in canon)}")
        print(f"               是否规范序的连续子序列: {'✓ 顺序正确' if contained else '✗ 顺序不符'}")
        print(f"               本段水位 → {wm}")

        mem = (await db.execute(
            select(AIMemory).where(AIMemory.session_id == uuid.UUID(str(sid)))
        )).scalar_one_or_none()
        print("\n[记忆回流] ai_memories:")
        if mem is None:
            print("  （无行 —— 画布会话轮数未达阈值 4，属预期）")
        else:
            print(f"  covered_rounds={mem.covered_rounds} summary={mem.summary[:80]}")

    print(f"\n[CLEANUP] 探针画布 id = {canvas_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
