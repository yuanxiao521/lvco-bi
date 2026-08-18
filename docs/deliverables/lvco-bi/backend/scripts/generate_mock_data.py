"""Generate mock datasets for BI testing: e-commerce + financial data.
Output: CSV files in ./mock_data/
Usage: python scripts/generate_mock_data.py
"""
import csv
import io
import os
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "mock_data"
OUT_DIR.mkdir(exist_ok=True)

random.seed(42)

# ---------------------------------------------------------------------------
# Shared lookup tables
# ---------------------------------------------------------------------------
REGIONS = ["North", "South", "East", "West", "Central"]
CITIES = {
    "North": ["Beijing", "Tianjin", "Shenyang"],
    "South": ["Shenzhen", "Guangzhou", "Xiamen"],
    "East": ["Shanghai", "Hangzhou", "Nanjing"],
    "West": ["Chengdu", "Chongqing", "Xi'an"],
    "Central": ["Wuhan", "Zhengzhou", "Changsha"],
}
CATEGORIES = ["Electronics", "Clothing", "Home & Garden", "Sports", "Books", "Food & Beverage"]
SUBCATEGORIES = {
    "Electronics": ["Smartphones", "Laptops", "Headphones", "Tablets", "Cameras"],
    "Clothing": ["Men's Wear", "Women's Wear", "Footwear", "Accessories", "Kids"],
    "Home & Garden": ["Furniture", "Kitchenware", "Decor", "Tools", "Lighting"],
    "Sports": ["Running", "Yoga", "Outdoor", "Fitness Equipment", "Swimming"],
    "Books": ["Fiction", "Non-Fiction", "Children", "Textbooks", "Comics"],
    "Food & Beverage": ["Snacks", "Beverages", "Organic", "Dairy", "Frozen"],
}
PRODUCT_POOL = [
    ("iPhone 15", "Electronics", "Smartphones", 999.00),
    ("Galaxy S24", "Electronics", "Smartphones", 899.00),
    ("MacBook Air", "Electronics", "Laptops", 1199.00),
    ("ThinkPad X1", "Electronics", "Laptops", 1399.00),
    ("AirPods Pro", "Electronics", "Headphones", 249.00),
    ("Sony WH-1000XM5", "Electronics", "Headphones", 349.00),
    ("iPad Air", "Electronics", "Tablets", 599.00),
    ("Canon EOS R6", "Electronics", "Cameras", 2499.00),
    ("Running Shoes Pro", "Sports", "Running", 149.00),
    ("Yoga Mat Premium", "Sports", "Yoga", 49.00),
    ("Camping Tent 4P", "Sports", "Outdoor", 299.00),
    ("Dumbbell Set 20kg", "Sports", "Fitness Equipment", 89.00),
    ("Denim Jacket", "Clothing", "Men's Wear", 79.00),
    ("Silk Dress", "Clothing", "Women's Wear", 129.00),
    ("Leather Boots", "Clothing", "Footwear", 159.00),
    ("Wool Scarf", "Clothing", "Accessories", 39.00),
    ("Standing Desk", "Home & Garden", "Furniture", 499.00),
    ("Ceramic Mug Set", "Home & Garden", "Kitchenware", 29.00),
    ("LED Floor Lamp", "Home & Garden", "Lighting", 89.00),
    ("The Great Novel", "Books", "Fiction", 24.99),
    ("Data Science 101", "Books", "Non-Fiction", 49.99),
    ("Organic Coffee Beans", "Food & Beverage", "Beverages", 19.99),
    ("Dark Chocolate Box", "Food & Beverage", "Snacks", 14.99),
]
PAYMENT_METHODS = ["Credit Card", "Alipay", "WeChat Pay", "Bank Transfer", "COD"]
CHANNELS = ["App", "Website", "Store", "Phone", "Third-party"]
STATUSES = ["Completed", "Processing", "Shipped", "Cancelled", "Returned"]

SEGMENTS = ["Premium", "Regular", "New", "At-Risk"]
LOYALTY_TIERS = ["Platinum", "Gold", "Silver", "Bronze"]

DEPARTMENTS = ["Sales", "Marketing", "R&D", "Operations", "Finance"]
ROLES = {
    "Sales": ["Sales Rep", "Account Manager", "Sales Director"],
    "Marketing": ["Marketing Specialist", "Brand Manager", "Campaign Manager"],
    "R&D": ["Engineer", "Product Manager", "Data Analyst"],
    "Operations": ["Ops Coordinator", "Logistics Manager", "Supply Chain"],
    "Finance": ["Accountant", "Financial Analyst", "CFO"],
}


def random_date(start: datetime, end: datetime) -> datetime:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, max(delta, 1)))


def csv_writer(path: str, headers: list[str], rows: list[list]):
    with open(OUT_DIR / path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    print(f"  → {path}  ({len(rows)} rows)")


# =========================================================================
# 1. ecommerce_orders.csv — ~1200 rows
# =========================================================================
print("\n[1/6] ecommerce_orders.csv")
headers = [
    "order_id", "order_date", "customer_name", "region", "city",
    "product_name", "category", "subcategory",
    "quantity", "unit_price", "total_amount",
    "payment_method", "channel", "status",
]
rows = []
start = datetime(2023, 1, 1)
end = datetime(2025, 12, 31)
for i in range(1200):
    region = random.choice(REGIONS)
    city = random.choice(CITIES[region])
    prod = random.choice(PRODUCT_POOL)
    qty = random.randint(1, 5)
    unit = round(prod[3] * random.uniform(0.85, 1.15), 2)
    total = round(qty * unit, 2)
    rows.append([
        f"ORD-{2023000000 + i + 1}",
        random_date(start, end).strftime("%Y-%m-%d"),
        f"Customer_{random.randint(1, 200)}",
        region, city,
        prod[0], prod[1], prod[2],
        qty, unit, total,
        random.choice(PAYMENT_METHODS),
        random.choice(CHANNELS),
        random.choice(STATUSES),
    ])
csv_writer("ecommerce_orders.csv", headers, rows)

# =========================================================================
# 2. financial_monthly.csv — 72 rows (6 years × 12 months)
# =========================================================================
print("\n[2/6] financial_monthly.csv")
headers = [
    "year_month", "revenue", "cost_of_goods", "gross_profit",
    "gross_margin_pct", "operating_expense", "marketing_expense",
    "r_and_d_expense", "net_profit", "net_margin_pct",
    "cash_flow", "region", "department", "budget", "budget_variance_pct",
]
rows = []
base_rev = 500000
for ym in range(2021, 2027):
    for m in range(1, 13):
        region = random.choice(REGIONS)
        dept = random.choice(DEPARTMENTS)
        # Seasonal + trend
        growth = 1 + (ym - 2021) * 0.12
        season = 1 + 0.15 * (1 if m in [11, 12] else -0.1 if m in [1, 2] else 0)
        rev = round(base_rev * growth * season * random.uniform(0.8, 1.2), 2)
        cogs = round(rev * random.uniform(0.45, 0.55), 2)
        gp = round(rev - cogs, 2)
        gm_pct = round(gp / rev * 100, 1) if rev else 0
        opex = round(rev * random.uniform(0.20, 0.30), 2)
        mkt = round(opex * random.uniform(0.3, 0.5), 2)
        rd = round(opex * random.uniform(0.1, 0.25), 2)
        np = round(gp - opex, 2)
        nm_pct = round(np / rev * 100, 1) if rev else 0
        cf = round(np + rev * random.uniform(0.02, 0.05), 2)
        budget = round(rev * random.uniform(0.9, 1.1), 2)
        bv_pct = round((rev - budget) / budget * 100, 1) if budget else 0
        rows.append([
            f"{ym}-{m:02d}", rev, cogs, gp, gm_pct,
            opex, mkt, rd, np, nm_pct,
            cf, region, dept, budget, bv_pct,
        ])
csv_writer("financial_monthly.csv", headers, rows)

# =========================================================================
# 3. product_performance.csv — ~300 rows
# =========================================================================
print("\n[3/6] product_performance.csv")
headers = [
    "product_id", "product_name", "category", "subcategory",
    "brand", "unit_price", "units_sold", "revenue",
    "avg_rating", "return_rate_pct", "stock_level", "season",
]
brands = ["TechPro", "SportMax", "HomeStyle", "ReadWell", "FreshFoods", "FashionPlus"]
seasons = ["Spring", "Summer", "Autumn", "Winter", "All-Year"]
rows = []
for i in range(300):
    prod = random.choice(PRODUCT_POOL)
    brand = random.choice(brands)
    unit = round(prod[3] * random.uniform(0.8, 1.3), 2)
    sold = random.randint(0, 5000)
    rev = round(sold * unit, 2)
    rating = round(random.uniform(3.0, 5.0), 2)
    returns = round(random.uniform(0.5, 12.0), 2)
    stock = random.randint(0, 2000)
    season = random.choice(seasons)
    rows.append([
        f"PROD-{i+1:04d}", prod[0], prod[1], prod[2],
        brand, unit, sold, rev, rating, returns, stock, season,
    ])
csv_writer("product_performance.csv", headers, rows)

# =========================================================================
# 4. customer_metrics.csv — ~600 rows
# =========================================================================
print("\n[4/6] customer_metrics.csv")
headers = [
    "customer_id", "customer_name", "region", "city",
    "segment", "acquisition_date", "total_orders", "total_spent",
    "avg_order_value", "last_order_date", "churn_risk", "loyalty_tier",
]
rows = []
acq_start = datetime(2021, 1, 1)
acq_end = datetime(2024, 6, 30)
last_end = datetime(2025, 12, 31)
for i in range(600):
    region = random.choice(REGIONS)
    city = random.choice(CITIES[region])
    segment = random.choice(SEGMENTS)
    acq_date = random_date(acq_start, acq_end).strftime("%Y-%m-%d")
    orders = random.randint(0, 80)
    avg_order = round(random.uniform(50, 800), 2) if orders > 0 else 0
    spent = round(orders * avg_order, 2)
    last_order = random_date(datetime(2024, 1, 1), last_end).strftime("%Y-%m-%d") if orders > 0 else ""
    churn = "High" if (orders == 0 or (orders < 5 and datetime.strptime(last_order, "%Y-%m-%d") < datetime(2024, 6, 1))) else "Low" if orders > 20 else "Medium"
    tier = random.choices(LOYALTY_TIERS, weights=[5, 15, 30, 50])[0] if orders > 0 else "Bronze"
    rows.append([
        f"CUST-{i+1:04d}", f"Customer_{i+1}", region, city,
        segment, acq_date, orders, spent, avg_order, last_order, churn, tier,
    ])
csv_writer("customer_metrics.csv", headers, rows)

# =========================================================================
# 5. marketing_campaigns.csv — ~250 rows
# =========================================================================
print("\n[5/6] marketing_campaigns.csv")
headers = [
    "campaign_id", "campaign_name", "channel", "start_date", "end_date",
    "budget", "spend", "impressions", "clicks", "conversions",
    "revenue_generated", "roi_pct", "region",
]
camp_types = [
    ("Double 11 Sale", "App"), ("618 Festival", "Website"), ("New Year Promo", "Store"),
    ("Spring Campaign", "Email"), ("Summer Sale", "Social Media"), ("Autumn Deal", "Third-party"),
    ("Black Friday", "App"), ("Membership Drive", "Website"), ("Brand Day", "Social Media"),
    ("Clearance", "Store"), ("Flash Sale", "App"), ("Referral Program", "Email"),
]
rows = []
for i in range(250):
    ct = random.choice(camp_types)
    start_d = random_date(datetime(2023, 1, 1), datetime(2025, 10, 1))
    duration = random.randint(7, 45)
    end_d = start_d + timedelta(days=duration)
    budget = round(random.uniform(5000, 200000), 2)
    spend = round(budget * random.uniform(0.7, 1.05), 2)
    impressions = int(spend * random.uniform(10, 200))
    ctr = random.uniform(0.005, 0.08)
    clicks = int(impressions * ctr)
    cv_rate = random.uniform(0.01, 0.1)
    conversions = int(clicks * cv_rate)
    rev = round(conversions * random.uniform(80, 500), 2)
    roi = round((rev - spend) / spend * 100, 1) if spend > 0 else 0
    rows.append([
        f"CAMP-{i+1:04d}", ct[0], ct[1],
        start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"),
        budget, spend, impressions, clicks, conversions, rev, roi,
        random.choice(REGIONS),
    ])
csv_writer("marketing_campaigns.csv", headers, rows)

# =========================================================================
# 6. employee_sales.csv — ~200 rows
# =========================================================================
print("\n[6/6] employee_sales.csv")
headers = [
    "employee_id", "name", "region", "department", "role",
    "hire_date", "monthly_target", "actual_sales", "achievement_pct",
    "commission", "deals_closed",
]
rows = []
for i in range(200):
    region = random.choice(REGIONS)
    dept = random.choice(DEPARTMENTS)
    role = random.choice(ROLES[dept])
    hire = random_date(datetime(2020, 1, 1), datetime(2024, 12, 31))
    target = round(random.uniform(30000, 200000), 2)
    ach_pct = round(random.uniform(40, 160), 1)
    actual = round(target * ach_pct / 100, 2)
    comm_rate = 0.05 if ach_pct >= 100 else 0.03
    commission = round(actual * comm_rate, 2)
    deals = round(actual / random.uniform(5000, 50000))
    rows.append([
        f"EMP-{i+1:04d}", f"Employee_{i+1}", region, dept, role,
        hire.strftime("%Y-%m-%d"), target, actual, ach_pct, commission, deals,
    ])
csv_writer("employee_sales.csv", headers, rows)

print(f"\nDone! {len(list(OUT_DIR.iterdir()))} CSV files written to {OUT_DIR}")
print("Next: upload via POST /api/v1/datasources/upload")
