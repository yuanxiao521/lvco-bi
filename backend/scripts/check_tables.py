"""Check test tables in PostgreSQL."""
import psycopg2

conn = psycopg2.connect(
    host="localhost", port=5432, user="lvco",
    password="lvco_secret", dbname="lvco_bi"
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='public' ORDER BY table_name"
)
tables = cur.fetchall()
for t in tables:
    cur.execute(f"SELECT COUNT(*) FROM {t[0]}")
    cnt = cur.fetchone()[0]
    print(f"  {t[0]}: {cnt} rows")
if not tables:
    print("  No test tables found!")

cur.close()
conn.close()
