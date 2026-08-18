"""Upload all mock CSVs to the Lvco BI backend.
Usage: python scripts/upload_mock_data.py [--email test@example.com] [--password test123]
"""
import argparse
import os
import sys
from pathlib import Path

import requests

BASE_URL = os.environ.get("LVCO_API_URL", "http://localhost:8000/api/v1")
NO_PROXY = {"proxies": {"http": None, "https": None}}  # bypass any proxy for localhost
MOCK_DIR = Path(__file__).resolve().parent.parent / "mock_data"

CSV_FILES = [
    "ecommerce_orders.csv",
    "financial_monthly.csv",
    "product_performance.csv",
    "customer_metrics.csv",
    "marketing_campaigns.csv",
    "employee_sales.csv",
]


def register_or_login(email: str, password: str) -> str:
    """Return access_token."""
    # Try login first
    login_resp = requests.post(
        f"{BASE_URL}/auth/login",
        json={"email": email, "password": password},
        **NO_PROXY,
    )
    if login_resp.status_code == 200:
        data = login_resp.json()
        token = data.get("data", {}).get("accessToken")
        if token:
            print(f"[OK] 登录成功: {email}")
            return token
        else:
            print(f"[!] 登录响应中未找到 token: {data}")
            sys.exit(1)

    # Register if not exists
    print(f"[*] 用户不存在，尝试注册: {email}")
    reg_resp = requests.post(
        f"{BASE_URL}/auth/register",
        json={"email": email, "password": password, "displayName": "Test User"},
        **NO_PROXY,
    )
    if reg_resp.status_code == 201:
        data = reg_resp.json()
        token = data.get("data", {}).get("accessToken")
        if token:
            print(f"[OK] 注册成功: {email}")
            return token

    print(f"[X] 注册失败: {reg_resp.status_code} {reg_resp.text}")
    sys.exit(1)


def upload_csv(token: str, name: str, filepath: Path) -> str | None:
    """Upload a CSV file; return datasource ID if success."""
    with open(filepath, "rb") as f:
        resp = requests.post(
            f"{BASE_URL}/datasources/upload",
            headers={"Authorization": f"Bearer {token}"},
            data={"name": name},
            files={"file": (filepath.name, f, "text/csv")},
            **NO_PROXY,
        )
    if resp.status_code == 201:
        ds_id = resp.json().get("data", {}).get("id")
        print(f"  ✓ {name}  →  {ds_id}")
        return ds_id

    detail = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
    print(f"  ✗ {name}  →  {resp.status_code} {detail}")
    return None


def main():
    global BASE_URL

    parser = argparse.ArgumentParser(description="Upload mock CSV data to Lvco BI")
    parser.add_argument("--email", default="test@example.com")
    parser.add_argument("--password", default="test123456")
    parser.add_argument("--base-url", default=BASE_URL, help="API base URL")
    args = parser.parse_args()

    BASE_URL = args.base_url.rstrip("/")

    # Health check (root path, not /api/v1)
    root_url = BASE_URL.rsplit("/api", 1)[0] if "/api" in BASE_URL else BASE_URL
    try:
        h = requests.get(f"{root_url}/health", timeout=5, **NO_PROXY)
        if h.status_code != 200:
            print(f"[X] Backend unhealthy: {h.status_code}")
            sys.exit(1)
    except requests.ConnectionError:
        print(f"[X] Cannot reach backend at {root_url}")
        print("    Make sure docker compose is running: docker compose up -d")
        sys.exit(1)

    print(f"[OK] Backend healthy: {root_url}")

    # Auth
    token = register_or_login(args.email, args.password)

    # Upload
    uploaded = 0
    for name in CSV_FILES:
        fp = MOCK_DIR / name
        if not fp.exists():
            print(f"  ! {name} — file not found, skipping")
            continue
        if upload_csv(token, name.replace(".csv", "").replace("_", " ").title(), fp):
            uploaded += 1

    print(f"\nDone! Uploaded {uploaded}/{len(CSV_FILES)} datasets.")


if __name__ == "__main__":
    main()
