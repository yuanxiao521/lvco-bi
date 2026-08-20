from typing import Any

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.schemas import CamelModel


class MeasureConfig(CamelModel):
    field: str = ""
    agg: str = "SUM"
    # 表达式度量（指标语义层）：直接给出聚合 SQL 表达式，如 'SUM("amount")'。
    # 非空时优先于 field+agg 使用；alias 可选，缺省由引擎从表达式推导。
    expression: str | None = None
    alias: str | None = None


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