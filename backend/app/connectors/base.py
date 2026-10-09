from abc import ABC, abstractmethod

from app.core.duckdb_client import DuckDBClient


class BaseDataSourceConnector(ABC):
    @abstractmethod
    def load_to_duckdb(self, client: DuckDBClient, file_path: str, schema_name: str) -> str:
        ...

    @abstractmethod
    def get_schema_meta(self, client: DuckDBClient, schema_name: str, table_name: str) -> dict:
        ...

    @abstractmethod
    def get_row_count(self, client: DuckDBClient, schema_name: str, table_name: str) -> int:
        ...
