#!/usr/bin/env python3
"""真实环境 e2e：LeadAgent 分析主链路（HTTP + SSE 全链路）。

链路：登录 → 列数据源 → POST /api/v1/ai/chat/stream（SSE）
     → 断言 LeadAgent 增量事件契约完整走通（分析主链路）：
       intent(analysis/data_qa) → decision(call_analysis/answer)
       → progress / tool_call / tool_result → done（正文非空, degraded=False）

前置条件（对照成功率高的已知清单）：
- 后端已启动（默认 http://127.0.0.1:8000，/docs 可开）
- backend/.env 已配 OPENAI_API_KEY 且 LEAD_AGENT_ENABLED=true（本测试的验证对象）
- 账号已有数据源（如 mock 数据脚本上传的 test 账号）

运行：
    python tests/real_e2e_lead_analysis.py
    python tests/real_e2e_lead_analysis.py --email xxx --password xxx --base-url http://127.0.0.1:8000

退出码：0=主链路全部断言通过；1=断言失败；2=环境问题（连不上/未配置）。
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

DEFAULT_EMAIL = "test@lvco.bi"
DEFAULT_PASSWORD = "test123"

API_PREFIX = "/api/v1"
REQUEST_TIMEOUT = 30   # 普通请求
SSE_TIMEOUT = 180      # 分析主链路（LLM+编排执行）可能较长


def _login(base: str, email: str, password: str) -> str:
    resp = requests.post(
        f"{base}{API_PREFIX}/auth/login",
        json={"email": email, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"[环境] 登录失败 {resp.status_code}: {resp.text[:200]}\n"
            f"      请确认账号 {email!r} 存在（可跑 scripts/upload_mock_data.py --email {email}）"
        )
    data = resp.json().get("data", {})
    token = (
        data.get("accessToken")
        or data.get("access_token")
        or ""
    )
    if not token:
        raise SystemExit(f"[环境] 登录响应里没找到 token: {json.dumps(data, ensure_ascii=False)[:200]}")
    return str(token)


def _pick_datasource(base: str, token: str) -> str:
    resp = requests.get(
        f"{base}{API_PREFIX}/datasources",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SystemExit(f"[环境] 列数据源失败 {resp.status_code}: {resp.text[:200]}")
    items = resp.json().get("data", {})
    # 兼容 {items:[...]} 或直接 [...]
    rows = items.get("items") if isinstance(items, dict) else items
    if not isinstance(rows, list) or not rows:
        raise SystemExit("[环境] 该账号没有数据源，先跑 scripts/upload_mock_data.py")
    first = rows[0]
    return str(first.get("id") or first.get("datasource_id"))


def _sse_events(base: str, token: str, ds_id: str) -> list[dict]:
    """发一条分析请求，流式收 SSE 事件。"""
    payload = {
        "message": "分析各地区销售总额的分布情况，按区域汇总并尽量生成图表",
        "datasourceId": ds_id,
    }
    resp = requests.post(
        f"{base}{API_PREFIX}/ai/chat/stream",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
        stream=True,
        timeout=SSE_TIMEOUT,
    )
    if resp.status_code != 200:
        body = resp.text[:300]
        if resp.status_code == 503 and "AI_NOT_CONFIGURED" in body:
            raise SystemExit("[环境] 后端未配置 LLM（OPENAI_API_KEY）")
        raise SystemExit(f"[环境] chat/stream 失败 {resp.status_code}: {body}")

    events: list[dict] = []
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        try:
            ev = json.loads(raw[len("data:"):].strip())
        except json.JSONDecodeError:
            continue
        events.append(ev)
    return events


def _render_timeline(events: list[dict]) -> str:
    lines: list[str] = []
    for i, ev in enumerate(events, 1):
        t = ev.get("type")
        if t == "session_created":
            lines.append(f"{i:>3} session_created")
        elif t == "intent":
            lines.append(f"{i:>3} intent       {ev.get('intent')} confidence={ev.get('confidence')} degraded={ev.get('degraded')}")
        elif t == "decision":
            lines.append(f"{i:>3} decision     {ev.get('action')} tool={ev.get('tool')} degraded={ev.get('degraded')}")
        elif t == "progress":
            lines.append(f"{i:>3} progress     [{ev.get('index')}/{ev.get('total')}] {ev.get('status')} {ev.get('title')}")
        elif t == "plan":
            lines.append(f"{i:>3} plan         steps={ev.get('steps')}")
        elif t == "tool_call":
            lines.append(f"{i:>3} tool_call    {ev.get('name')}")
        elif t == "tool_result":
            lines.append(f"{i:>3} tool_result  {ev.get('name')}")
        elif t == "message":
            lines.append(f"{i:>3} message      delta={len(ev.get('delta',''))}字符")
        elif t == "done":
            lines.append(f"{i:>3} done         charts={len(ev.get('charts') or [])} degraded={ev.get('degraded')}")
        elif t == "error":
            lines.append(f"{i:>3} error        {ev.get('message')}")
        else:
            lines.append(f"{i:>3} {t}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="LeadAgent 分析主链路真实 e2e")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    print(f"目标后端: {args.base_url}")
    print(f"账号: {args.email}")

    token = _login(args.base_url, args.email, args.password)
    ds_id = _pick_datasource(args.base_url, token)
    print(f"选中数据源: {ds_id}\n")

    events = _sse_events(args.base_url, token, ds_id)
    if not events:
        raise SystemExit("[环境] 流为空（未收到任何 SSE 事件）")

    print("── LeadAgent 事件时间线 ──")
    print(_render_timeline(events))
    print("")

    # ── 断言：分析主链路契约 ──
    intents = [ev for ev in events if ev.get("type") == "intent"]
    decisions = [ev for ev in events if ev.get("type") == "decision"]
    progresses = [ev for ev in events if ev.get("type") == "progress"]
    done_evs = [ev for ev in events if ev.get("type") == "done"]
    err_evs = [ev for ev in events if ev.get("type") == "error"]
    body = "".join(ev.get("delta", "") for ev in events if ev.get("type") == "message")

    results: list[tuple[str, bool, str]] = []
    results.append(
        ("intent 事件（LeadAgent 特有）", bool(intents),
         f"intent={intents[0].get('intent')}" if intents else "缺失——未走主导 Agent 链路？")
    )
    results.append(
        ("decision 事件（LeadAgent 特有）", bool(decisions),
         f"action={decisions[0].get('action')}" if decisions else "缺失")
    )
    correct_intent = bool(intents) and intents[0].get("intent") in ("analysis", "data_qa")
    results.append(
        ("意图命中分析类", correct_intent,
         f"{intents[0].get('intent')}" if intents else "-")
    )
    results.append(
        ("决策为执行类（call_analysis/answer 均可）",
         bool(decisions) and decisions[0].get("action") in ("call_analysis", "answer"),
         f"{decisions[0].get('action')}" if decisions else "-")
    )
    results.append(("progress 进度事件", bool(progresses), f"{len(progresses)} 条"))
    results.append(("最终 done 事件", bool(done_evs), ""))
    done_ok = bool(done_evs) and done_evs[0].get("degraded") in (None, False)
    results.append(("done 未降级（degraded=False）", done_ok,
                    f"degraded={done_evs[0].get('degraded')}" if done_evs else "-"))
    results.append(("报告正文非空", len(body.strip()) > 0, f"{len(body.strip())} 字符"))
    results.append(("无 error 事件", not err_evs, "-"))

    print("── 断言结果 ──")
    failed = 0
    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name}" + (f"（{detail}）" if detail else ""))
        if not ok:
            failed += 1

    print("\n结论：" + ("主链路全通 ✅" if failed == 0 else f"共 {failed} 项未通过"))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.ConnectionError:
        raise SystemExit("[环境] 连不上后端，请先启动 uvicorn（port 8000）")