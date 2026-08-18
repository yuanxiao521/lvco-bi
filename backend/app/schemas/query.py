from typing import Any

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas import CamelModel


class MeasureConfig(CamelModel):
    field: str
    agg: str


class FilterConfig(CamelModel):
    field: str
    op: str
    value: Any


class SortConfig(CamelModel):
    field: str
    order: str = "desc"


class ChartQueryConfig(CamelModel):
    dimensions: list[str]
    measures: list[MeasureConfig]
    filters: list[FilterConfig] = Field(default_factory=list)
    chart_type: str | None = None
    datasource_id: str | None = None
    sort: SortConfig | None = None
    limit: int = Field(default=1000, ge=1, le=10000)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class QueryResult(CamelModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    chart_type: str | None = None
    query_time_ms: int