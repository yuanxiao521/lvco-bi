"""Quick validation for statistics.py refactor and connector _infer_category."""
from uuid import uuid4
from unittest.mock import MagicMock

from app.api.v1.statistics import _resolve_table_ref
from app.models.datasource import SourceType
from app.connectors.csv_connector import CSVConnector
from app.connectors.postgres_connector import _infer_category as pg_infer


def test_resolve_table_ref():
    uid = uuid4()

    # CSV
    ds_csv = MagicMock()
    ds_csv.id = uuid4()
    ds_csv.source_type = SourceType.csv
    ds_csv.schema_meta = {"fields": []}
    _, ref = _resolve_table_ref(ds_csv, uid)
    assert ref.endswith('."data"'), f"CSV fallback should use .data, got {ref}"
    print("OK  CSV ->", ref)

    # PostgreSQL
    ds_pg = MagicMock()
    ds_pg.id = uuid4()
    ds_pg.source_type = SourceType.postgresql
    ds_pg.schema_meta = {"fields": [], "table_name": "orders"}
    _, ref = _resolve_table_ref(ds_pg, uid)
    assert 'public."orders"' in ref, f"PG should use public.orders, got {ref}"
    print("OK  PG  ->", ref)

    # PostgreSQL fallback
    ds_pg2 = MagicMock()
    ds_pg2.id = uuid4()
    ds_pg2.source_type = SourceType.postgresql
    ds_pg2.schema_meta = {"fields": []}
    _, ref = _resolve_table_ref(ds_pg2, uid)
    assert 'public."data"' in ref, f"PG fallback should use public.data, got {ref}"
    print("OK  PG0 ->", ref)

    # MySQL
    ds_mysql = MagicMock()
    ds_mysql.id = uuid4()
    ds_mysql.source_type = SourceType.mysql
    ds_mysql.schema_meta = {"fields": [], "table_name": "sales"}
    _, ref = _resolve_table_ref(ds_mysql, uid)
    assert ref.endswith('."sales"') and "public" not in ref, f"MySQL should not use public, got {ref}"
    print("OK  MyS ->", ref)


def test_infer_category():
    csv = CSVConnector()

    # 含 id 的数值列应被识别为 measure
    assert csv._infer_category("user_id", "INTEGER") == "measure", "user_id INTEGER should be measure"
    assert csv._infer_category("follower_count", "BIGINT") == "measure"
    assert csv._infer_category("unit_price", "DOUBLE") == "measure"
    assert csv._infer_category("order_date", "TIMESTAMP") == "time"
    assert csv._infer_category("user_name", "VARCHAR") == "dimension"
    assert csv._infer_category("product_id", "VARCHAR") == "key", "string id column should be key"
    print("OK  CSV _infer_category")

    assert pg_infer("video_id", "BIGINT") == "measure"
    assert pg_infer("order_id", "INT4") == "measure"
    assert pg_infer("uuid", "UUID") == "key"
    assert pg_infer("created_at", "TIMESTAMPTZ") == "time"
    print("OK  PG  _infer_category")


if __name__ == "__main__":
    test_resolve_table_ref()
    test_infer_category()
    print("ALL OK")
