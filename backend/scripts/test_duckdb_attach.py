"""Test DuckDB attach to PostgreSQL and schema extraction."""
import duckdb

# Connect to DuckDB in-memory
con = duckdb.connect()

print("Testing DuckDB PostgreSQL ATTACH...")

# ATTACH PostgreSQL
attach_sql = "ATTACH 'host=localhost port=5432 user=lvco password=lvco_secret dbname=lvco_bi' AS pg_test (TYPE postgres, READ_ONLY)"
try:
    con.execute(attach_sql)
    print("  ATTACH successful")
except Exception as e:
    print(f"  ATTACH FAILED: {e}")
    exit(1)

# List tables
try:
    tables = con.execute("SELECT table_name FROM pg_test.information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").fetchall()
    print(f"  tables via information_schema: {[t[0] for t in tables]}")
except Exception as e:
    print(f"  information_schema query FAILED: {e}")

# Try pg_catalog
try:
    tables2 = con.execute("SELECT tablename FROM pg_test.pg_catalog.pg_tables WHERE schemaname = 'public'").fetchall()
    print(f"  tables via pg_catalog: {[t[0] for t in tables2]}")
except Exception as e:
    print(f"  pg_catalog query FAILED: {e}")

# Try DESCRIBE on a specific table
try:
    desc = con.execute("DESCRIBE pg_test.public.ecommerce_orders").fetchall()
    print(f"\n  DESCRIBE ecommerce_orders:")
    for row in desc:
        print(f"    {row}")
except Exception as e:
    print(f"  DESCRIBE FAILED: {e}")

# Try information_schema.columns
try:
    cols = con.execute(
        "SELECT column_name, data_type FROM pg_test.information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'ecommerce_orders'"
    ).fetchall()
    print(f"\n  information_schema.columns for ecommerce_orders:")
    for row in cols:
        print(f"    {row}")
except Exception as e:
    print(f"  information_schema.columns FAILED: {e}")

# Try direct query
try:
    con.execute("SELECT * FROM pg_test.public.ecommerce_orders LIMIT 1").fetchall()
    print("\n  Direct SELECT from ecommerce_orders: OK")
except Exception as e:
    print(f"  Direct SELECT FAILED: {e}")

# Try COUNT
try:
    cnt = con.execute("SELECT COUNT(*) FROM pg_test.public.ecommerce_orders").fetchone()
    print(f"  COUNT ecommerce_orders: {cnt[0]}")
except Exception as e:
    print(f"  COUNT FAILED: {e}")

con.close()
print("\nDone!")
