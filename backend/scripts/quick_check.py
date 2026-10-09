"""Quick check: list datasources status."""
import requests
import json

NO_PROXY = {"proxies": {"http": None, "https": None}}
r = requests.post("http://localhost:8000/api/v1/auth/login",
    json={"email": "test@example.com", "password": "test123456"}, **NO_PROXY)
token = r.json()["data"]["accessToken"]
headers = {"Authorization": f"Bearer {token}"}

r = requests.get("http://localhost:8000/api/v1/datasources", headers=headers, **NO_PROXY)
items = r.json()["data"]["items"]
print(f"Found {len(items)} datasources:")
for item in items:
    sm = item.get("schemaMeta", {}) or {}
    fields = sm.get("fields", [])
    err = sm.get("error", "")
    print(f"  {item['name']}: status={item['status']} fields={len(fields)} rows={item.get('rowCount',0)}", end="")
    if err:
        print(f" ERROR: {err}", end="")
    print()
