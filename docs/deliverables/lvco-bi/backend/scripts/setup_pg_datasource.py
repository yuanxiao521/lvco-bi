"""Create test tables & data in PostgreSQL, then register datasource via API.
Usage: python scripts/setup_pg_datasource.py
"""
import random
import os
from datetime import datetime, timedelta

import psycopg2
import requests

random.seed(42)

PG_CONFIG = {
    "host": os.environ.get("PG_HOST", "localhost"),
    "port": int(os.environ.get("PG_PORT", 5432)),
    "user": os.environ.get("PG_USER", "lvco"),
    "password": os.environ.get("PG_PASSWORD", "lvco_secret"),
    "dbname": os.environ.get("PG_DB", "lvco_bi"),
}

API_BASE = "http://localhost:8000/api/v1"
NO_PROXY = {"proxies": {"http": None, "https": None}}

# ---------------------------------------------------------------------------
# Shared data
# ---------------------------------------------------------------------------
REGIONS = ["North", "South", "East", "West", "Central"]
CITIES = {
    "North": ["Beijing", "Tianjin", "Shenyang"],
    "South": ["Shenzhen", "Guangzhou", "Xiamen"],
    "East": ["Shanghai", "Hangzhou", "Nanjing"],
    "West": ["Chengdu", "Chongqing", "Xi'an"],
    "Central": ["Wuhan", "Zhengzhou", "Changsha"],
}
PRODUCTS = [
    ("iPhone 15", "Electronics", "Smartphones", 999),
    ("Galaxy S24", "Electronics", "Smartphones", 899),
    ("MacBook Air", "Electronics", "Laptops", 1199),
    ("ThinkPad X1", "Electronics", "Laptops", 1399),
    ("AirPods Pro", "Electronics", "Headphones", 249),
    ("Running Shoes Pro", "Sports", "Running", 149),
    ("Yoga Mat Premium", "Sports", "Yoga", 49),
    ("Denim Jacket", "Clothing", "Men's Wear", 79),
    ("Silk Dress", "Clothing", "Women's Wear", 129),
    ("Standing Desk", "Home & Garden", "Furniture", 499),
    ("Ceramic Mug Set", "Home & Garden", "Kitchenware", 29),
    ("The Great Novel", "Books", "Fiction", 25),
    ("Organic Coffee Beans", "Food & Beverage", "Beverages", 20),
    ("Dark Chocolate Box", "Food & Beverage", "Snacks", 15),
]
DEPARTMENTS = ["Sales", "Marketing", "R&D", "Operations", "Finance"]


def rdate(start, end):
    d = (end - start).days
    return start + timedelta(days=random.randint(0, max(d, 1)))


conn = psycopg2.connect(**PG_CONFIG)
conn.autocommit = True
cur = conn.cursor()

# Drop existing test tables
for t in ["ecommerce_orders", "financial_monthly", "product_performance",
           "customer_metrics", "marketing_campaigns", "employee_sales"]:
    cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    print(f"  Dropped {t}")

# ---------------------------------------------------------------------------
# 1. ecommerce_orders
# ---------------------------------------------------------------------------
print("\n[1/6] Creating ecommerce_orders...")
cur.execute("""
    CREATE TABLE ecommerce_orders (
        order_id VARCHAR PRIMARY KEY,
        order_date DATE NOT NULL,
        customer_name VARCHAR NOT NULL,
        region VARCHAR NOT NULL,
        city VARCHAR NOT NULL,
        product_name VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        subcategory VARCHAR NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price NUMERIC(10,2) NOT NULL,
        total_amount NUMERIC(12,2) NOT NULL,
        payment_method VARCHAR NOT NULL,
        channel VARCHAR NOT NULL,
        status VARCHAR NOT NULL
    )
""")
rows = []
start, end = datetime(2023, 1, 1), datetime(2025, 12, 31)
pms = ["Credit Card", "Alipay", "WeChat Pay", "Bank Transfer", "COD"]
chs = ["App", "Website", "Store", "Phone", "Third-party"]
sts = ["Completed", "Processing", "Shipped", "Cancelled", "Returned"]
for i in range(1200):
    region = random.choice(REGIONS)
    city = random.choice(CITIES[region])
    prod = random.choice(PRODUCTS)
    qty = random.randint(1, 5)
    unit = round(prod[3] * random.uniform(0.85, 1.15), 2)
    total = round(qty * unit, 2)
    rows.append((f"ORD-{2023000000+i+1}", rdate(start, end), f"Customer_{random.randint(1,200)}",
                  region, city, prod[0], prod[1], prod[2], qty, unit, total,
                  random.choice(pms), random.choice(chs), random.choice(sts)))
cur.executemany("INSERT INTO ecommerce_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
print(f"  Inserted {len(rows)} rows")

# ---------------------------------------------------------------------------
# 2. financial_monthly
# ---------------------------------------------------------------------------
print("\n[2/6] Creating financial_monthly...")
cur.execute("""
    CREATE TABLE financial_monthly (
        year_month VARCHAR PRIMARY KEY,
        revenue NUMERIC(14,2) NOT NULL,
        cost_of_goods NUMERIC(14,2) NOT NULL,
        gross_profit NUMERIC(14,2) NOT NULL,
        gross_margin_pct NUMERIC(5,1) NOT NULL,
        operating_expense NUMERIC(14,2) NOT NULL,
        marketing_expense NUMERIC(14,2) NOT NULL,
        r_and_d_expense NUMERIC(14,2) NOT NULL,
        net_profit NUMERIC(14,2) NOT NULL,
        net_margin_pct NUMERIC(5,1) NOT NULL,
        cash_flow NUMERIC(14,2) NOT NULL,
        region VARCHAR NOT NULL,
        department VARCHAR NOT NULL,
        budget NUMERIC(14,2) NOT NULL,
        budget_variance_pct NUMERIC(5,1) NOT NULL
    )
""")
rows = []
base_rev = 500000
for ym in range(2021, 2027):
    for m in range(1, 13):
        region = random.choice(REGIONS)
        dept = random.choice(DEPARTMENTS)
        growth = 1 + (ym - 2021) * 0.12
        season = 1 + 0.15 * (1 if m in [11, 12] else -0.1 if m in [1, 2] else 0)
        rev = round(base_rev * growth * season * random.uniform(0.8, 1.2), 2)
        cogs = round(rev * random.uniform(0.45, 0.55), 2)
        gp = round(rev - cogs, 2)
        gm_pct = round(gp / rev * 100, 1) if rev else 0
        opex = round(rev * random.uniform(0.20, 0.30), 2)
        mkt = round(opex * random.uniform(0.3, 0.5), 2)
        rd = round(opex * random.uniform(0.1, 0.25), 2)
        np_ = round(gp - opex, 2)
        nm_pct = round(np_ / rev * 100, 1) if rev else 0
        cf = round(np_ + rev * random.uniform(0.02, 0.05), 2)
        budget = round(rev * random.uniform(0.9, 1.1), 2)
        bv_pct = round((rev - budget) / budget * 100, 1) if budget else 0
        rows.append((f"{ym}-{m:02d}", rev, cogs, gp, gm_pct, opex, mkt, rd, np_, nm_pct, cf, region, dept, budget, bv_pct))
cur.executemany("INSERT INTO financial_monthly VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
print(f"  Inserted {len(rows)} rows")

# ---------------------------------------------------------------------------
# 3. product_performance
# ---------------------------------------------------------------------------
print("\n[3/6] Creating product_performance...")
cur.execute("""
    CREATE TABLE product_performance (
        product_id VARCHAR PRIMARY KEY,
        product_name VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        subcategory VARCHAR NOT NULL,
        brand VARCHAR NOT NULL,
        unit_price NUMERIC(10,2) NOT NULL,
        units_sold INTEGER NOT NULL,
        revenue NUMERIC(14,2) NOT NULL,
        avg_rating NUMERIC(3,2) NOT NULL,
        return_rate_pct NUMERIC(5,2) NOT NULL,
        stock_level INTEGER NOT NULL,
        season VARCHAR NOT NULL
    )
""")
brands = ["TechPro", "SportMax", "HomeStyle", "ReadWell", "FreshFoods", "FashionPlus"]
seasons = ["Spring", "Summer", "Autumn", "Winter", "All-Year"]
rows = []
for i in range(300):
    prod = random.choice(PRODUCTS)
    unit = round(prod[3] * random.uniform(0.8, 1.3), 2)
    sold = random.randint(0, 5000)
    rev = round(sold * unit, 2)
    rows.append((f"PROD-{i+1:04d}", prod[0], prod[1], prod[2], random.choice(brands),
                  unit, sold, rev, round(random.uniform(3.0, 5.0), 2),
                  round(random.uniform(0.5, 12.0), 2), random.randint(0, 2000),
                  random.choice(seasons)))
cur.executemany("INSERT INTO product_performance VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
print(f"  Inserted {len(rows)} rows")

# ---------------------------------------------------------------------------
# 4. customer_metrics
# ---------------------------------------------------------------------------
print("\n[4/6] Creating customer_metrics...")
cur.execute("""
    CREATE TABLE customer_metrics (
        customer_id VARCHAR PRIMARY KEY,
        customer_name VARCHAR NOT NULL,
        region VARCHAR NOT NULL,
        city VARCHAR NOT NULL,
        segment VARCHAR NOT NULL,
        acquisition_date DATE NOT NULL,
        total_orders INTEGER NOT NULL,
        total_spent NUMERIC(12,2) NOT NULL,
        avg_order_value NUMERIC(10,2) NOT NULL,
        last_order_date DATE,
        churn_risk VARCHAR,
        loyalty_tier VARCHAR NOT NULL
    )
""")
segments = ["Premium", "Regular", "New", "At-Risk"]
tiers = ["Platinum", "Gold", "Silver", "Bronze"]
acq_start, acq_end = datetime(2021, 1, 1), datetime(2024, 6, 30)
last_end = datetime(2025, 12, 31)
rows = []
for i in range(600):
    region = random.choice(REGIONS)
    city = random.choice(CITIES[region])
    orders_n = random.randint(0, 80)
    aov = round(random.uniform(50, 800), 2) if orders_n > 0 else 0
    spent = round(orders_n * aov, 2)
    lo = rdate(datetime(2024, 1, 1), last_end) if orders_n > 0 else None
    churn = "High" if (orders_n == 0 or (orders_n < 5 and lo and lo < datetime(2024, 6, 1))) else ("Low" if orders_n > 20 else "Medium")
    tier = random.choices(tiers, weights=[5, 15, 30, 50])[0] if orders_n > 0 else "Bronze"
    rows.append((f"CUST-{i+1:04d}", f"Customer_{i+1}", region, city, random.choice(segments),
                  rdate(acq_start, acq_end), orders_n, spent, aov, lo, churn, tier))
cur.executemany("INSERT INTO customer_metrics VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
print(f"  Inserted {len(rows)} rows")

# ---------------------------------------------------------------------------
# 5. marketing_campaigns
# ---------------------------------------------------------------------------
print("\n[5/6] Creating marketing_campaigns...")
cur.execute("""
    CREATE TABLE marketing_campaigns (
        campaign_id VARCHAR PRIMARY KEY,
        campaign_name VARCHAR NOT NULL,
        channel VARCHAR NOT NULL,
        start_date DATE NOT NULL,
        end_date DATE NOT NULL,
        budget NUMERIC(12,2) NOT NULL,
        spend NUMERIC(12,2) NOT NULL,
        impressions INTEGER NOT NULL,
        clicks INTEGER NOT NULL,
        conversions INTEGER NOT NULL,
        revenue_generated NUMERIC(14,2) NOT NULL,
        roi_pct NUMERIC(6,1) NOT NULL,
        region VARCHAR NOT NULL
    )
""")
camp_types = [("Double 11 Sale", "App"), ("618 Festival", "Website"), ("Summer Sale", "Social Media"),
              ("Black Friday", "App"), ("Flash Sale", "App"), ("Spring Campaign", "Email")]
rows = []
for i in range(250):
    ct = random.choice(camp_types)
    sd = rdate(datetime(2023, 1, 1), datetime(2025, 10, 1))
    ed = sd + timedelta(days=random.randint(7, 45))
    budget = round(random.uniform(5000, 200000), 2)
    spend = round(budget * random.uniform(0.7, 1.05), 2)
    imp = int(spend * random.uniform(10, 200))
    clicks_n = int(imp * random.uniform(0.005, 0.08))
    cv = int(clicks_n * random.uniform(0.01, 0.1))
    rev = round(cv * random.uniform(80, 500), 2)
    roi = round((rev - spend) / spend * 100, 1) if spend > 0 else 0
    rows.append((f"CAMP-{i+1:04d}", ct[0], ct[1], sd, ed, budget, spend, imp, clicks_n, cv, rev, roi, random.choice(REGIONS)))
cur.executemany("INSERT INTO marketing_campaigns VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
print(f"  Inserted {len(rows)} rows")

# ---------------------------------------------------------------------------
# 6. employee_sales
# ---------------------------------------------------------------------------
print("\n[6/6] Creating employee_sales...")
cur.execute("""
    CREATE TABLE employee_sales (
        employee_id VARCHAR PRIMARY KEY,
        name VARCHAR NOT NULL,
        region VARCHAR NOT NULL,
        department VARCHAR NOT NULL,
        role VARCHAR NOT NULL,
        hire_date DATE NOT NULL,
        monthly_target NUMERIC(12,2) NOT NULL,
        actual_sales NUMERIC(12,2) NOT NULL,
        achievement_pct NUMERIC(5,1) NOT NULL,
        commission NUMERIC(10,2) NOT NULL,
        deals_closed INTEGER NOT NULL
    )
""")
roles_map = {"Sales": ["Sales Rep", "Account Manager", "Sales Director"],
             "Marketing": ["Marketing Specialist", "Brand Manager"],
             "R&D": ["Engineer", "Product Manager", "Data Analyst"],
             "Operations": ["Ops Coordinator", "Logistics Manager"],
             "Finance": ["Accountant", "Financial Analyst"]}
rows = []
for i in range(200):
    region = random.choice(REGIONS)
    dept = random.choice(DEPARTMENTS)
    role = random.choice(roles_map[dept])
    hire = rdate(datetime(2020, 1, 1), datetime(2024, 12, 31))
    target = round(random.uniform(30000, 200000), 2)
    ach_pct = round(random.uniform(40, 160), 1)
    actual = round(target * ach_pct / 100, 2)
    comm_rate = 0.05 if ach_pct >= 100 else 0.03
    rows.append((f"EMP-{i+1:04d}", f"Employee_{i+1}", region, dept, role, hire,
                  target, actual, ach_pct, round(actual * comm_rate, 2),
                  round(actual / random.uniform(5000, 50000))))
cur.executemany("INSERT INTO employee_sales VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)
print(f"  Inserted {len(rows)} rows")

cur.close()
conn.close()

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
vconn = psycopg2.connect(**PG_CONFIG)
vconn.autocommit = True
vcur = vconn.cursor()
vcur.execute("SELECT table_name, pg_size_pretty(pg_total_relation_size(quote_ident(table_name))) FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'ecommerce%' OR table_name='financial_monthly' OR table_name='product_performance' OR table_name='customer_metrics' OR table_name='marketing_campaigns' OR table_name='employee_sales' ORDER BY table_name")
print(f"\n{'Table':<25} {'Size':<12} {'Rows'}")
print("-" * 50)
for tname, tsize in sorted(vcur.fetchall()):
    vcur.execute(f"SELECT COUNT(*) FROM {tname}")
    cnt = vcur.fetchone()[0]
    print(f"{tname:<25} {tsize:<12} {cnt}")
vcur.close()
vconn.close()
print("\nAll tables created successfully!")
