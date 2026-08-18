"""Re-register PG datasources against backend on port 8001 and sync them."""
import requests
import sys

NO_PROXY = {"proxies": {"http": None, "https": None}}
API = "http://127.0.0.1:8001/api/v1"

# Try common admin credentials first, fall back to test user
candidates = [
    ("admin@lvcom.com", "admin123"),
    ("test@lvcom.com", "changeme123"),
    ("admin@lvcom.com", "admin"),
    ("admin@lvcom.com", "changeme123"),
]

token = None
for email, pwd in candidates:
    r = requests.post(
        f"{API}/auth/login",
        json={"email": email, "password": pwd},
        **NO_PROXY,
    )
    if r.status_code == 200:
        token = r.json()["data"]["accessToken"]
        print(f"[auth] logged in as {email}")
        break
    print(f"[auth] {email}: {r.status_code} {r.text[:120]}")

if not token:
    print("[auth] no credentials worked, aborting")
    sys.exit(1)

headers = {"Authorization": f"Bearer {token}"}

# Delete existing datasources
r = requests.get(f"{API}/datasources", headers=headers, **NO_PROXY)
items = r.json()["data"]["items"]
print(f"[cleanup] {len(items)} existing datasources")
for item in items:
    requests.delete(f"{API}/datasources/{item['id']}", headers=headers, **NO_PROXY)

tables = [
    "ecommerce_orders",
    "financial_monthly",
    "product_performance",
    "customer_metrics",
    "marketing_campaigns",
    "employee_sales",
]

for tname in tables:
    r = requests.post(
        f"{API}/datasources/connect",
        json={
            "name": tname.replace("_", " ").title(),
            "sourceType": "postgresql",
            "host": "localhost",
            "port": 5432,
            "dbName": "lvco_bi",
            "username": "lvco",
            "password": "lvco_secret",
            "tableName": tname,
        },
        headers=headers,
        **NO_PROXY,
    )
    if r.status_code != 201:
        print(f"  {tname}: CREATE FAILED {r.status_code} {r.text[:200]}")
        continue
    dsid = r.json()["data"]["id"]

    sr = requests.post(f"{API}/datasources/{dsid}/sync", headers=headers, **NO_PROXY)
    if sr.status_code == 200:
        data = sr.json()["data"]
        fields = len((data.get("schemaMeta") or {}).get("fields", []))
        rows = data.get("rowCount", 0)
        status = data.get("status", "?")
        print(f"  {tname}: status={status} fields={fields} rows={rows}")
    else:
        print(f"  {tname}: SYNC FAILED {sr.status_code} {sr.text[:200]}")

print("\nDone! Reload data source page in browser.")
