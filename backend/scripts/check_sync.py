"""Test sync a single datasource via API and check results."""
import requests

NO_PROXY = {"proxies": {"http": None, "https": None}}
API = "http://localhost:8000/api/v1"

# Login
r = requests.post(
    f"{API}/auth/login",
    json={"email": "test@example.com", "password": "test123456"},
    **NO_PROXY,
)
token = r.json()["data"]["accessToken"]
headers = {"Authorization": f"Bearer {token}"}

# List datasources
r = requests.get(f"{API}/datasources", headers=headers, **NO_PROXY)
items = r.json()["data"]["items"]
print(f"Found {len(items)} datasources:")
for item in items:
    print(f"  {item['name']}: id={item['id']}, status={item.get('status','?')}")
    if item.get('schemaMeta'):
        sm = item['schemaMeta']
        if isinstance(sm, dict):
            fields = sm.get('fields', [])
            print(f"    fields={len(fields)}, table_name={sm.get('table_name','?')}, rowCount={item.get('rowCount',0)}")
            if 'error' in sm:
                print(f"    ERROR: {sm['error']}")
            if fields:
                for f in fields[:3]:
                    print(f"      {f['name']} ({f['data_type']}) [{f['category']}]")
