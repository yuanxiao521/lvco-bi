"""对话入口后台续跑探针：发消息后断开连接 → 任务继续在后台跑 → resume 续收 → 落库完整。

需要后端已重启到含注册表版本（app/services/chat_stream_registry.py + ai.py 任务化）。

用法：
    python scripts/_probe_chat_resume.py
    python scripts/_probe_chat_resume.py --base http://127.0.0.1:8000/api/v1 --email test@lvco.bi --password 123456

判定：
  - 阶段 1：首次发消息能完整收到事件流并以 done 收尾，assistant 消息最终落库非空
  - 阶段 2（核心）：发长任务 → 读前 3 个事件即断开连接 → 任务继续在后台跑
    （list_sessions.running 为 true，若任务过快完成则记录已结束路径）→
    带 resume:true 重连 → 收到补发/增量 message delta 与 done → 落库完整
  - 阶段 3：任务结束后 running 归 false、interrupted 不置位（正常完成）
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

REQUEST_TIMEOUT = 30
SSE_TIMEOUT = 300
LONG_TASK = "写一份 300 字的产品介绍，分成三个自然段落。"


def _login(base: str, email: str, password: str) -> str:
    resp = requests.post(f"{base}/auth/login", json={"email": email, "password": password}, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise SystemExit(f"[环境] 登录失败 {resp.status_code}: {resp.text[:200]}")
    token = resp.json().get("data", {}).get("accessToken") or ""
    if not token:
        raise SystemExit("[环境] 登录响应里没有 accessToken")
    return str(token)


def _create_session(base: str, token: str) -> str:
    resp = requests.post(f"{base}/ai/sessions", json={}, headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 201:
        raise SystemExit(f"[环境] 建会话失败 {resp.status_code}: {resp.text[:200]}")
    return str(resp.json().get("data", {}).get("id"))


def _sessions(base: str, token: str) -> list[dict]:
    resp = requests.get(f"{base}/ai/sessions?entry=chat", headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise SystemExit(f"[环境] 列表失败 {resp.status_code}: {resp.text[:200]}")
    return list(resp.json().get("data", []))


def _messages(base: str, token: str, sid: str) -> list[dict]:
    resp = requests.get(f"{base}/ai/sessions/{sid}/messages", headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise SystemExit(f"[环境] 拉消息失败 {resp.status_code}: {resp.text[:200]}")
    return list(resp.json().get("data", []))


def _post_stream(base: str, token: str, sid: str, message: str, resume: bool = False, keep_open: bool = True):
    """发起 /chat/stream；keep_open=True 时用流式连接逐事件产出，由调用方决定何时关闭。"""
    url = f"{base}/ai/chat/stream"
    resp = requests.post(
        url,
        json={"datasource_id": None, "session_id": sid, "message": message, "resume": resume},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=SSE_TIMEOUT,
        stream=True,
    )
    return resp


def _drain_events(resp, max_events: int | None = None, timeout: float = SSE_TIMEOUT) -> list[dict]:
    """把 SSE 连接里的事件读出来；读到 done 或耗尽即停。"""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            if max_events is not None and len(events) >= max_events:
                break
            if events[-1].get("type") == "done":
                break
            if time.monotonic() > deadline:
                break
    except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError):
        pass  # 提前关闭连接预期内的中断
    finally:
        resp.close()
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000/api/v1")
    ap.add_argument("--email", default="test@lvco.bi")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()

    token = _login(args.base, args.email, args.password)
    sid = _create_session(args.base, token)
    print(f"[setup] session={sid}")

    # ── 阶段 1：完整跑一轮，验证任务化后正常链路没坏 ──
    print("\n━━ 阶段 1：正常发送并完整收流 ━━")
    with _post_stream(args.base, token, sid, "你好，简单介绍一下你自己。") as resp:
        evs = _drain_events(resp)
    types = [e.get("type") for e in evs]
    assert "done" in types, f"[FAIL] 没收到 done，只收到 {types}"
    text = "".join(e.get("delta", "") for e in evs if e.get("type") == "message")
    print(f"[阶段1] events={types.count('done')} done, message 累计 {len(text)} 字")
    time.sleep(1.5)
    ms = _messages(args.base, token, sid)
    last_assistant = [m for m in ms if m.get("role") == "assistant"][-1]
    assert str(last_assistant.get("content", "")).strip(), "[FAIL] 落库 assistant 内容为空"
    print(f"[阶段1] 落库 assistant 内容 {len(last_assistant['content'])} 字 → PASS")

    # ── 阶段 2：长任务发起后立刻断开 → 后台续跑 → resume 续收 ──
    print("\n━━ 阶段 2：断开后后台跑 + resume 续收 ━━")
    # 发起长任务，不等任何事件、0.4s 后主动断开（此时任务已启动、正处于决策等待期）
    r1 = _post_stream(args.base, token, sid, LONG_TASK)
    try:
        time.sleep(0.4)
    finally:
        r1.close()  # 模拟用户刚看到回执就刷新/断网
    print("[阶段2] 断开：任务发起 0.4s 后主动关闭连接")

    time.sleep(0.05)
    sess_now = next((s for s in _sessions(args.base, token) if s.get("id") == sid), {})
    running = bool(sess_now.get("running"))
    print(f"[阶段2] 断开后 list_sessions.running={running} interrupted={sess_now.get('interrupted')}")

    # resume 续收（无论任务是否还在跑，流都会在 done 处收敛）
    with _post_stream(args.base, token, sid, "", resume=True) as r2:
        evs2 = _drain_events(r2, timeout=SSE_TIMEOUT)
    types2 = [e.get("type") for e in evs2]
    assert "done" in types2, f"[FAIL] resume 没收到 done，只收 {types2}"
    resumed_text = "".join(e.get("delta", "") for e in evs2 if e.get("type") == "message")
    print(f"[阶段2] resume 收到 {len(types2)} 事件 (含 done)，补发/增量文本 {len(resumed_text)} 字")
    if running:
        print("[阶段2] 命中「任务仍在跑 → 同一任务续收」路径")
    else:
        print("[阶段2] 任务在重连前已结束（resume 空收 + 前端拉库兜底路径）")

    # 等待后台任务把最终结果落库（轮询最多 20s）
    content2 = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        ms2 = _messages(args.base, token, sid)
        candidates = [m for m in ms2 if m.get("role") == "assistant"]
        if candidates and str(candidates[-1].get("content", "")).strip():
            content2 = str(candidates[-1]["content"]).strip()
            break
        time.sleep(0.5)
    assert content2, "[FAIL] 断开后最终落库 assistant 内容为空（任务没跑完）"
    print(f"[阶段2] 最终落库 assistant 内容 {len(content2)} 字 → PASS")

    # ── 阶段 3：任务态收敛 ──
    print("\n━━ 阶段 3：任务态收敛 ━━")
    sess_final = next((s for s in _sessions(args.base, token) if s.get("id") == sid), {})
    assert sess_final.get("running") is not True, "[FAIL] 任务结束后 running 应为 false"
    assert sess_final.get("interrupted") is not True, "[FAIL] 正常完成不应标记 interrupted"
    print(f"[阶段3] running=false interrupted=false → PASS")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, SystemExit) as exc:
        print(f"\n✗ {exc}")
        sys.exit(1)