import requests, json

NO_PROXY = {"proxies": {"http": None, "https": None}}
r = requests.post(
    "http://localhost:8000/api/v1/auth/login",
    json={"email": "test@example.com", "password": "test123456"},
    **NO_PROXY,
)
token = r.json()["data"]["accessToken"]
r2 = requests.get(
    "http://localhost:8000/api/v1/datasources",
    headers={"Authorization": f"Bearer {token}"},
    **NO_PROXY,
)
items = r2.json()["data"]["items"]
print(f"Total: {len(items)} datasources\n")
for item in items:
    sm = item.get("schemaMeta")
    fields_count = len(sm.get("fields", [])) if sm else 0
    table_name = sm.get("table_name", "N/A") if sm else "N/A"
    available = sm.get("available_tables", []) if sm else []
    name = item.get("name")
    rc = item.get("rowCount")
    print(f"  {name}")
    print(f"    fields={fields_count} table={table_name} available_tables={available[:3]}...")
    print(f"    row_count={rc}")
    if sm:
        print(f"    schema_meta keys: {list(sm.keys())}")
