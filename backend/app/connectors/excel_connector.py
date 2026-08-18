import csv
import os
import tempfile
from datetime import date, datetime

import openpyxl

from app.connectors.base import BaseDataSourceConnector
from app.core.duckdb_client import DuckDBClient


class ExcelConnector(BaseDataSourceConnector):
    def load_to_duckdb(self, client: DuckDBClient, file_path: str, schema_name: str) -> str:
        table_name = "data"
        client.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')

        # openpyxl 完整读取 xlsx，避免 st_read (GDAL) 默认 25 行限制
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError("Excel 文件为空")
        headers = rows[0]
        data_rows = rows[1:]

        # 写临时 CSV，再用 DuckDB read_csv_auto 加载（自动推导类型）
        fd, tmp_path = tempfile.mkstemp(suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerows(data_rows)

            safe_path = tmp_path.replace("\\", "/")
            client.execute(
                f'CREATE OR REPLACE TABLE "{schema_name}"."{table_name}" AS '
                f"SELECT * FROM read_csv_auto('{safe_path}', header=true, auto_detect=true)"
            )
        finally:
            os.unlink(tmp_path)

        wb.close()
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
        # ID, key, code, uuid 优先判为 key，避免数值型 ID 被误判为 measure
        if any(kw in lower_name for kw in ("id", "key", "code", "uuid")):
            return "key"
        # 数值类型归为 measure
        if data_type in ("BIGINT", "INTEGER", "DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL",
                          "HUGEINT", "SMALLINT", "TINYINT"):
            return "measure"
        if data_type in ("DATE", "TIMESTAMP", "TIMESTAMPTZ"):
            return "time"
        return "dimension"

    def _get_sample_values(self, client: DuckDBClient, schema_name: str, table_name: str, col_name: str) -> list:
        result = client.fetchall(
            f'SELECT DISTINCT "{col_name}" FROM "{schema_name}"."{table_name}" WHERE "{col_name}" IS NOT NULL LIMIT 3'
        )
        return [row[0] for row in result]
