"""Bind template metrics to Ecommerce Orders datasource via API."""
import requests

BASE = "http://localhost:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}

# Login
resp = requests.post(f"{BASE}/auth/login", json={"email": "test@lvco.bi", "password": "123456"}, **NO_PROXY)
token = resp.json()["data"]["accessToken"]
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Find Ecommerce Orders datasource
resp = requests.get(f"{BASE}/datasources", headers=headers, params={"pageSize": 100}, **NO_PROXY)
ds_list = resp.json().get("data", {}).get("items", [])
ecommerce_ds = next((ds for ds in ds_list if "ecommerce" in ds.get("name", "").lower()), None)
if not ecommerce_ds:
    print("[X] Ecommerce Orders 数据源不存在")
    exit(1)
ds_id = ecommerce_ds["id"]
print(f"[OK] 数据源: {ecommerce_ds['name']} (id={ds_id})")

# List metrics
resp = requests.get(f"{BASE}/metrics", headers=headers, **NO_PROXY)
metrics = resp.json().get("data", [])

# Formula mapping
FORMULAS = {
    "sales_amount": ('SUM("total_amount")', "SUM"),
    "order_count": ('COUNT("order_id")', "COUNT"),
    "customer_count": ('COUNT(DISTINCT "customer_name")', "COUNT_DISTINCT"),
    "avg_price": ('AVG("unit_price")', "AVG"),
}

for m in metrics:
    key = m.get("key")
    if key not in FORMULAS:
        continue
    formula, agg = FORMULAS[key]
    metric_id = m["id"]
    resp = requests.patch(
        f"{BASE}/metrics/{metric_id}",
        headers=headers,
        json={"formula": formula, "aggKind": agg, "datasourceId": ds_id},
        **NO_PROXY,
    )
    if resp.status_code == 200:
        print(f"  ✓ {key}: formula={formula}, agg={agg}")
    else:
        print(f"  ✗ {key}: {resp.status_code} {resp.text}")

# Verify
resp = requests.get(f"{BASE}/metrics", headers=headers, **NO_PROXY)
metrics = resp.json().get("data", [])
print("\n验证:")
for m in metrics:
    key = m.get("key")
    if key in FORMULAS:
        print(f"  {key}: datasourceId={m.get('datasourceId')}, formula={m.get('formula')}, aggKind={m.get('aggKind')}")
