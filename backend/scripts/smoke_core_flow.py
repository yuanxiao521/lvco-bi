"""核心业务链路 API 冒烟排查脚本。

遍历登录 → 令牌 → 数据源 → 字段 → 指标 → 画布 → 图表配置 → 仪表盘 → 洞察 → 通知，
逐个状态码/字段做健康检查，失败的项目打印响应体便于定位。

用法：
    python scripts/smoke_core_flow.py              # 默认 http://localhost:8000
    python scripts/smoke_core_flow.py --base http://localhost:8001 --email x@x.com --password p
"""
from __future__ import annotations

import argparse
import sys

import httpx

P = lambda *a: print(*a)  # noqa: E731


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--email", default="test@lvco.bi")
    ap.add_argument("--password", default="123456")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    c = httpx.Client(base_url=base, timeout=20.0)

    ok = 0
    fail = []

    def check(name: str, resp: httpx.Response, *preds) -> None:
        nonlocal ok
        good = resp.status_code < 400 and all(p(resp) for p in preds)
        if good:
            ok += 1
            P(f"  [PASS] {name}")
        else:
            fail.append(name)
            P(f"  [FAIL] {name} -> {resp.status_code}")
            P(f"         body: {resp.text[:300]}")

    # ---- 1. 登录 ----
    r = c.post("/api/v1/auth/login", json={"email": args.email, "password": args.password})
    check("auth/login", r, lambda x: x.json().get("data", {}).get("accessToken"))
    token = r.json().get("data", {}).get("accessToken", "")
    h = {"Authorization": f"Bearer {token}"}

    # ---- 2. 数据源 ----
    r = c.get("/api/v1/datasources", headers=h, params={"pageSize": 100})
    check("datasources/list", r, lambda x: x.status_code < 400)
    ds_list = r.json().get("data", {}).get("items", [])
    ds = next((d for d in ds_list if d.get("type") != "POSTGRES"), None)
    if ds is None and ds_list:
        ds = ds_list[0]
    check("datasources/non_null", r, lambda x: len(ds_list) > 0)
    ds_id = ds["id"] if ds else None

    # ---- 3. 指标 ----
    r = c.get("/api/v1/metrics", headers=h)
    check("metrics/list", r, lambda x: x.status_code < 400)

    # ---- 4. 仪表盘 ----
    r = c.get("/api/v1/dashboards", headers=h)
    check("dashboards/list", r, lambda x: x.status_code < 400)

    # ---- 5. 通知未读数 ----
    r = c.get("/api/v1/notifications/unread_count", headers=h)
    check("notifications/unread_count", r, lambda x: x.status_code < 400)

    # ---- 6. 洞察 ----
    r = c.get("/api/v1/insights", headers=h)
    check("insights/list", r, lambda x: x.status_code < 400)

    # ---- 7. 审计 ----
    r = c.get("/api/v1/audit", headers=h)
    check("audit/list", r, lambda x: x.status_code < 400)

    # ---- 8. 回收站 ----
    r = c.get("/api/v1/trash", headers=h)
    check("trash/list", r, lambda x: x.status_code < 400)

    # ---- 9. 报表 ----
    r = c.get("/api/v1/reports", headers=h)
    check("reports/list", r, lambda x: x.status_code < 400)

    # ---- 10. 权限 ----
    r = c.get("/api/v1/permissions", headers=h)
    check("permissions/list", r, lambda x: x.status_code < 400)

    # ---- 11. 统计接口（若有数据源）----
    if ds_id:
        r = c.post(
            "/api/v1/statistics/describe",
            headers=h,
            json={"datasource_id": str(ds_id)},
        )
        check("statistics/describe", r, lambda x: x.status_code < 400)

    P(f"\n结果: {ok} 通过, {len(fail)} 失败")
    if fail:
        P("失败项: " + ", ".join(fail))
        sys.exit(1)


if __name__ == "__main__":
    main()