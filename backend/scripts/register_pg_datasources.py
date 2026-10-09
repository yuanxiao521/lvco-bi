"""Register PostgreSQL tables as datasources and sync them."""
import requests

NO_PROXY = {"proxies": {"http": None, "https": None}}
API = "http://localhost:8000/api/v1"

# Register or login
r = requests.post(
    f"{API}/auth/register",
    json={"email": "test@example.com", "password": "test123456", "displayName": "Test User"},
    **NO_PROXY,
)
if r.status_code != 201:
    r = requests.post(
        f"{API}/auth/login",
        json={"email": "test@example.com", "password": "test123456"},
        **NO_PROXY,
    )
token = r.json()["data"]["accessToken"]
headers = {"Authorization": f"Bearer {token}"}

# Delete existing datasources
r = requests.get(f"{API}/datasources", headers=headers, **NO_PROXY)
items = r.json()["data"]["items"]
for item in items:
    did = item["id"]
    requests.delete(f"{API}/datasources/{did}", headers=headers, **NO_PROXY)
    print(f"  Deleted: {item['name']}")

tables = [
    "ecommerce_orders",
    "financial_monthly",
    "product_performance",
    "customer_metrics",
    "marketing_campaigns",
    "employee_sales",
]

for tname in tables:
    # Create datasource
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

    # Sync
    sr = requests.post(f"{API}/datasources/{dsid}/sync", headers=headers, **NO_PROXY)
    if sr.status_code == 200:
        data = sr.json()["data"]
        print(f"  {tname}: synced | fields={len(data.get('schemaMeta',{}).get('fields',[]))} rows={data.get('rowCount',0)}")
    else:
        print(f"  {tname}: SYNC FAILED {sr.status_code} {sr.text[:200]}")

print("\nDone!")
