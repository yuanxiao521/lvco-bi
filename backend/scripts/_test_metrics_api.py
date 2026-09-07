"""Quick test: call metrics API and print response."""
import requests

BASE = "http://localhost:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}

# Login
resp = requests.post(f"{BASE}/auth/login", json={"email": "test@lvco.bi", "password": "123456"}, **NO_PROXY)
token = resp.json()["data"]["accessToken"]
headers = {"Authorization": f"Bearer {token}"}

# List metrics
resp = requests.get(f"{BASE}/metrics", headers=headers, **NO_PROXY)
data = resp.json()
import json
print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
