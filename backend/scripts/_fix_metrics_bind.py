"""Fix: bind 4 template metrics to Ecommerce Orders datasource with real formulas."""
import asyncio
from sqlalchemy import text
from app.core.database import engine

TEST_USER_ID = "2aace5b5-57d4-42d6-b90d-8d3e9b97ecb1"  # test@lvco.bi

# 公式映射：用 CSV 实际列名
METRIC_FORMULAS = {
    "sales_amount": ('SUM("total_amount")', "SUM"),
    "order_count": ('COUNT("order_id")', "COUNT"),
    "customer_count": ('COUNT(DISTINCT "customer_name")', "COUNT_DISTINCT"),
    "avg_price": ('AVG("unit_price")', "AVG"),
}


async def main():
    async with engine.begin() as conn:
        # 1. 找到 Ecommerce Orders 数据源 ID
        r = await conn.execute(
            text("SELECT id FROM datasources WHERE name = 'Ecommerce Orders' ORDER BY created_at DESC LIMIT 1")
        )
        row = r.fetchone()
        if not row:
            print("[X] Ecommerce Orders 数据源不存在")
            return
        ds_id = row[0]
        print(f"[OK] 数据源 ID: {ds_id}")

        # 2. 删除重复的模板指标（user_id=NULL），保留有 user_id 的
        for key in METRIC_FORMULAS.keys():
            result = await conn.execute(
                text("DELETE FROM metric_definitions WHERE key = :key AND user_id IS NULL"),
                {"key": key},
            )
            print(f"  删除重复模板指标 {key}: rows={result.rowcount}")

        # 3. 更新剩余指标：设置 datasource_id + formula
        for key, (formula, agg) in METRIC_FORMULAS.items():
            result = await conn.execute(
                text(
                    "UPDATE metric_definitions "
                    "SET datasource_id = :ds_id, "
                    "    formula = :formula, agg_kind = :agg "
                    "WHERE key = :key AND user_id = :user_id"
                ),
                {"ds_id": ds_id, "formula": formula, "agg": agg, "key": key, "user_id": TEST_USER_ID},
            )
            print(f"  更新 {key}: rows={result.rowcount}, formula={formula}")

    # 验证
    async with engine.connect() as conn:
        r = await conn.execute(text(
            "SELECT key, name, user_id, datasource_id, formula, agg_kind "
            "FROM metric_definitions ORDER BY name"
        ))
        print("\n更新后指标:")
        for row in r.fetchall():
            print(f"  key={row[0]}, name={row[1]}, user_id={row[2]}, ds_id={row[3]}, formula={row[4]}, agg={row[5]}")


asyncio.run(main())
