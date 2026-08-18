from datetime import date, datetime

from app.connectors.base import BaseDataSourceConnector
from app.core.duckdb_client import DuckDBClient


class CSVConnector(BaseDataSourceConnector):
    def load_to_duckdb(self, client: DuckDBClient, file_path: str, schema_name: str) -> str:
        table_name = "data"
        client.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
        safe_path = file_path.replace("\\", "/")
        client.execute(
            f'CREATE OR REPLACE TABLE "{schema_name}"."{table_name}" AS '
            f"SELECT * FROM read_csv_auto('{safe_path}', header=true, auto_detect=true, sample_size=100000)"
        )
        return table_name

    def get_schema_meta(self, client: DuckDBClient, schema_name: str, table_name: str) -> dict:
        rows = client.fetchall(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ?",
            [schema_name, table_name],
        )
        fields = []
        for col_name, data_type in rows:
            category = self._infer_category(col_name, data_type)
            sample_values = self._get_sample_values(client, schema_name, table_name, col_name)
            fields.append({
                "name": col_name,
                "data_type": data_type,
                "nullable": True,
                "sample": [self._json_safe(v) for v in sample_values],
                "category": category,
                "display_name": col_name,
            })
        return {"fields": fields}

    def get_row_count(self, client: DuckDBClient, schema_name: str, table_name: str) -> int:
        result = client.fetchall(f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"')
        return result[0][0] if result else 0

    @staticmethod
    def _json_safe(value):
        """Convert Python date/datetime to ISO string for JSON serialization."""
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return value

    def _infer_category(self, col_name: str, data_type: str) -> str:
        lower_name = col_name.lower()
        # 优先级：先按 data_type 判断(最准确)，再用列名兜底
        # 数值类型一律归为 measure，避免 video_id/user_id 等含 id 的数值列被错判为 key
        if data_type in ("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
                          "HUGEINT", "SMALLINT", "TINYINT"):
            return "measure"
        if data_type in ("DATE", "TIMESTAMP", "TIMESTAMPTZ"):
            return "time"
        # 非数值列里再按列名推断 key（避免 follower_id 这类字符串 ID 被错判为 dimension）
        if any(kw in lower_name for kw in ("id", "key", "code", "uuid")):
            return "key"
        return "dimension"

    def _get_sample_values(self, client: DuckDBClient, schema_name: str, table_name: str, col_name: str) -> list:
        result = client.fetchall(
            f'SELECT DISTINCT "{col_name}" FROM "{schema_name}"."{table_name}" WHERE "{col_name}" IS NOT NULL LIMIT 3'
        )
        return [row[0] for row in result]
