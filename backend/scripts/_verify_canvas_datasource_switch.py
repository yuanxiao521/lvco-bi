"""验证画布换源接口 PATCH /canvases/{id}（datasourceId）：
1. 用临时画布测（不动用户已有画布）
2. 只传 title：老行为不回归
3. 传合法 datasourceId：返回体 datasourceId 已变，且 DB 复核
4. 传别人的/随机 datasourceId：404 拒收
5. 清理临时画布
"""
from __future__ import annotations

import sys
import uuid

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8001/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}
EMAIL, PASSWORD = "test@lvco.bi", "123456"


def main() -> None:
    tok = requests.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD},
                        **NO_PROXY, timeout=15).json()["data"]["accessToken"]
    H = {"Authorization": f"Bearer {tok}"}

    ds = requests.get(f"{BASE}/datasources?page=1&page_size=5", headers=H, **NO_PROXY, timeout=15).json()
    items = ds["data"]["items"] if isinstance(ds.get("data"), dict) else ds["data"]
    assert items, ds
    ds_id = items[0]["id"]
    print("datasource:", ds_id, items[0].get("name"))

    # 临时画布
    created = requests.post(f"{BASE}/canvases", headers=H, json={"title": "换源验证-临时"}, **NO_PROXY, timeout=15).json()["data"]
    cid = created["id"]
    print("temp canvas:", cid, "datasourceId=", created.get("datasourceId"))
    ok = True
    try:
        # 1) 只改标题（老行为）
        r = requests.patch(f"{BASE}/canvases/{cid}", headers=H, json={"title": "换源验证-改名"},
                           **NO_PROXY, timeout=15)
        print("[1] 只改标题:", r.status_code, r.json()["data"]["title"])
        ok &= r.status_code == 200 and r.json()["data"]["title"] == "换源验证-改名"

        # 2) 换源
        r = requests.patch(f"{BASE}/canvases/{cid}", headers=H, json={"datasourceId": ds_id},
                           **NO_PROXY, timeout=15)
        got = r.json()["data"]["datasourceId"] if r.status_code == 200 else None
        print("[2] 换源:", r.status_code, "返回 datasourceId=", got)
        ok &= r.status_code == 200 and got == ds_id

        # 3) DB 复核（详情接口）
        r = requests.get(f"{BASE}/canvases/{cid}", headers=H, **NO_PROXY, timeout=15)
        print("[3] 详情复核 datasourceId=", r.json()["data"]["datasourceId"])
        ok &= r.json()["data"]["datasourceId"] == ds_id

        # 4) 非法数据源（随机 UUID）应 404
        r = requests.patch(f"{BASE}/canvases/{cid}", headers=H,
                           json={"datasourceId": str(uuid.uuid4())}, **NO_PROXY, timeout=15)
        print("[4] 随机数据源:", r.status_code, str(r.json())[:90])
        ok &= r.status_code == 404

        # 5) 不传任何字段：应原样返回（不再 422）
        r = requests.patch(f"{BASE}/canvases/{cid}", headers=H, json={}, **NO_PROXY, timeout=15)
        print("[5] 空 body:", r.status_code)
        ok &= r.status_code == 200
    finally:
        d = requests.delete(f"{BASE}/canvases/{cid}", headers=H, **NO_PROXY, timeout=15)
        print("清理临时画布:", d.status_code)

    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()