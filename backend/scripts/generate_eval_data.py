"""生成与 agent_evals dataset.jsonl 匹配的 ecommerce_orders 评测数据。

字段规格对齐 25 道题的 expected_sql_template：
order_date/amount/region(中文)/customer_id/customer_name/product_name/category/channel/
quantity/age_group/gender/first_order_date/refund_date/refund_amount
覆盖最近 12 个月（含当前月），保证本月/近30天/近6个月/季度对比等题目有数据。
"""
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parent / "mock_data_eval" / "ecommerce_orders.csv"
OUT.parent.mkdir(parents=True, exist_ok=True)
random.seed(2026)

REGIONS = ["华东", "华北", "华南", "西部", "东北", "华中"]
CITIES = {
    "华东": ["上海", "杭州", "南京", "苏州"],
    "华北": ["北京", "天津", "石家庄", "济南"],
    "华南": ["广州", "深圳", "厦门", "福州"],
    "西部": ["成都", "重庆", "西安", "昆明"],
    "东北": ["沈阳", "大连", "哈尔滨", "长春"],
    "华中": ["武汉", "长沙", "郑州", "南昌"],
}
CATEGORIES = ["电子产品", "服装鞋帽", "家居用品", "食品饮料", "图书文具", "运动户外"]
PRODUCTS = {
    "电子产品": ["智能手机", "笔记本电脑", "平板电脑", "蓝牙耳机", "智能手表"],
    "服装鞋帽": ["T恤", "牛仔裤", "运动鞋", "羽绒服", "连衣裙"],
    "家居用品": ["沙发", "餐桌", "台灯", "床垫", "收纳箱"],
    "食品饮料": ["咖啡豆", "茶叶", "坚果礼盒", "矿泉水", "巧克力"],
    "图书文具": ["小说", "教材", "钢笔", "笔记本", "台历"],
    "运动户外": ["跑步机", "瑜伽垫", "帐篷", "哑铃", "自行车"],
}
CHANNELS = ["线上", "线下"]
AGE_GROUPS = ["18-25", "26-35", "36-45", "46-60", "60+"]
GENDERS = ["男", "女"]
STATUSES = ["已完成", "已发货", "待发货", "已退款"]

def random_date(start: datetime, end: datetime) -> datetime:
    delta = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, delta))

# 覆盖最近 12 个月，且当前月有数据
now = datetime.now()
start = now - timedelta(days=365)

headers = [
    "order_id", "order_date", "customer_id", "customer_name", "region", "city",
    "product_name", "category", "channel", "quantity", "unit_price", "amount",
    "payment_method", "status", "age_group", "gender", "first_order_date",
    "refund_date", "refund_amount",
]
rows = []
for i in range(1500):
    region = random.choice(REGIONS)
    city = random.choice(CITIES[region])
    category = random.choice(CATEGORIES)
    product = random.choice(PRODUCTS[category])
    qty = random.randint(1, 5)
    unit = round(random.uniform(20, 2000), 2)
    amount = round(qty * unit, 2)
    odate = random_date(start, now)
    first_order = random_date(start - timedelta(days=700), odate)
    refunded = random.random() < 0.15
    refund_amount = round(amount * random.uniform(0.3, 1.0), 2) if refunded else None
    refund_date = (odate + timedelta(days=random.randint(1, 14))).strftime("%Y-%m-%d") if refunded else None
    rows.append([
        f"ORD-{2026000000 + i}", odate.strftime("%Y-%m-%d"),
        f"CUST-{random.randint(1001, 1600)}", f"客户{random.randint(1001, 1600)}",
        region, city, product, category, random.choice(CHANNELS), qty, unit, amount,
        random.choice(["支付宝", "微信", "银行卡", "货到付款"]), random.choice(STATUSES),
        random.choice(AGE_GROUPS), random.choice(GENDERS), first_order.strftime("%Y-%m-%d"),
        refund_date, refund_amount,
    ])

with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(headers)
    w.writerows(rows)
print("WROTE", OUT, len(rows), "rows")

# 校验：近 30 天是否有数据、本月是否有数据
from collections import Counter
dates = [r[1] for r in rows]
last30 = sum(1 for d in dates if d >= (now - timedelta(days=30)).strftime("%Y-%m-%d"))
cur_month = now.strftime("%Y-%m")
month_cnt = sum(1 for d in dates if d.startswith(cur_month))
print("rows_last30d:", last30, "| rows_current_month:", month_cnt, "| cur_month:", cur_month)
assert last30 > 0 and month_cnt > 0, "评测题目需要本月/近30天数据"