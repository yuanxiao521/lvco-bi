"""Step 3 E2E 手动回归：走真实后端 /api/v1/ai/chat/stream，观察真实选型与执行（临时脚本）。

验证点：
1. 常规聚合（对比/排名）→ planner 与 executor 应选 query_engine 且执行成功出图
2. 时间趋势 → 观察真实行为（query_engine 借道 or query_datasource）
3. tool_result 是否 error / 是否有 chart 事件
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

BASE = "http://127.0.0.1:8000/api/v1"

SAMPLE_CSV = (
    "order_id,order_date,category,region,amount,quantity\n"
    + "\n".join(
        f"O{i:04d},2026-0{(i % 3) + 6}-{(i % 28) + 1:02d},"
        f"{['手机', '家电', '服装', '食品'][i % 4]},{['华东', '华北', '华南', '西南'][i % 4]},"
        f"{(i * 37) % 5000 + 50},{(i % 5) + 1}"
        for i in range(1, 61)
    )
)

QUERIES = [
    ("对比各内容分类的粉丝总量", "expect query_engine + 出图"),
    ("统计各城市获赞总数，按获赞数降序排列", "expect query_engine + 出图（顺带验证 sort 修复）"),
    ("统计男女博主的平均粉丝数", "expect query_engine + 出图"),
]


def parse_sse_lines(text: str) -> list[dict]:
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


async def main() -> None:
    client = httpx.AsyncClient(base_url=BASE, timeout=180.0)
    # 1. 登录
    r = await client.post("/auth/login", json={"email": "test@lvco.bi", "password": "123456"})
    if r.status_code >= 400:
        print(f"登录失败 {r.status_code}: {r.text[:200]}，请检查账号。")
        return
    token = r.json()["data"]["accessToken"]
    h = {"Authorization": f"Bearer {token}"}

    # 2. 数据源：取第一个非 POSTGRES；没有则上传样本 CSV
    r = await client.get("/datasources", headers=h, params={"pageSize": 100})
    items = r.json().get("data", {}).get("items", [])
    ds = next((d for d in items if d.get("type") != "POSTGRES"), None)
    if ds is None:
        files = {"file": ("probe_orders.csv", SAMPLE_CSV.encode("utf-8"), "text/csv")}
        r = await client.post("/datasources/upload", headers=h, data={"name": "probe_电商订单"}, files=files)
        if r.status_code >= 400:
            print(f"上传失败 {r.status_code}: {r.text[:300]}")
            return
        ds = r.json().get("data", {})
        print(f"[上传] datasource_id={ds.get('id')}")
    ds_id = str(ds["id"])
    print(f"[数据源] {ds.get('name')} id={ds_id}")

    # 3. 逐条真实聊天
    q_filter = sys.argv[1] if len(sys.argv) > 1 else None
    force_ds = sys.argv[2] if len(sys.argv) > 2 else ""
    if force_ds:
        ds = next((d for d in items if str(d.get("id")) == force_ds), None)
        if ds is None:
            print(f"未找到数据源 {force_ds}，可用: {[d.get('id') for d in items][:5]}")
            return
        ds_id = force_ds  # 真正用于请求的数据源 id
        print(f"[数据源(指定)] {ds.get('name')} id={ds_id}")
    else:
        print(f"[数据源] {ds.get('name')} id={ds_id}")
    for q, expect in QUERIES:
        if q_filter and q_filter not in q:
            continue
        print(f"\n{'='*60}\n[Q] {q}\n    预期: {expect}")
        try:
            async with client.stream(
                "POST", "/ai/chat/stream",
                headers=h,
                json={"datasourceId": ds_id, "datasource_id": ds_id, "message": q},
            ) as resp:
                body = (await resp.aread()).decode("utf-8", errors="replace")
            if resp.status_code >= 400:
                print(f"    HTTP {resp.status_code}: {body[:200]}")
                continue
            events = parse_sse_lines(body)
        except Exception as e:
            print(f"    请求异常: {e}")
            continue

        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        tool_results = [e for e in events if e.get("type") == "tool_result"]
        plans = [e for e in events if e.get("type") == "plan"]
        done_events = [e for e in events if e.get("type") == "done"]
        charts = next((e.get("charts", []) for e in done_events if e.get("charts")), None)
        # 正文 = message.delta（去掉 "> 查询失败" 之类的旁白行）
        msg_deltas = [e.get("delta", "") for e in events if e.get("type") == "message"]
        body_text = "".join(d for d in msg_deltas if not d.strip().startswith(">")).strip()
        err_narrations = [d for d in msg_deltas if d.strip().startswith(">")]

        if plans:
            pt = [s.get("tool") for s in plans[0].get("plan", {}).get("steps", [])]
            print(f"    [plan] {pt}")
        seq = []
        for tc in tool_calls:
            name = tc.get("name", "?")
            err = "?"
            for tr in tool_results:
                if tr.get("name") == name:
                    try:
                        obj = json.loads(tr.get("result", "{}"))
                        if "error" in obj:
                            err = "error"
                            print(f"    [err!] {name}: {str(obj.get('error'))[:160]} | hint={str(obj.get('hint'))[:100]}")
                        else:
                            err = "ok"
                            print(f"    [ok]   {name}: columns={obj.get('columns')} rows={len(obj.get('rows') or [])}")
                    except Exception:
                        err = "parse?"
                    break
            seq.append(f"{name}({err})")
        print(f"    [tools] {' → '.join(seq)}")
        chart_types = [c.get("chart_type") for c in (charts or [])]
        print(f"    [charts] {chart_types if chart_types else '无'}  [报告字数] {len(body_text)}")
        if err_narrations:
            print(f"    [旁白提示] {err_narrations}")
        if body_text:
            print(f"    [报告 前120字] {body_text[:120]}")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

