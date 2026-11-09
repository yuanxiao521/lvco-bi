"""指标级范围口径（metric_filters）：指标自带的一组 WHERE 条件。

为什么需要
----------
指标公式是纯聚合表达式，表达能力里没有"范围"：仅已支付 / 剔除退款单 /
只看华东 这类口径此前只能塞进 CASE WHEN（污染公式、且每个指标各写各的）。
业界（dbt MetricFlow measure.filters / LookML measure filters）都把范围
做成指标的一等公民：口径 = 聚合 + 过滤，二者一起版本化、一起血缘。

语义约定
--------
- 编译期：resolve_metric 把 filters 追加到 ChartQueryConfig.filters 的**最前面**
  （指标口径优先于调用方临时过滤，后者仍可继续收紧）。
- op 白名单与 query_engine.ALLOWED_OPERATORS 完全一致，不发明新算子。
- 派生指标 metric("key") 引用的是**纯聚合**，不继承被引指标的 filter
  （引用方想带范围就在自己的 filters 里写）——与 docs/research/08 的结论一致。
"""
import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MetricFilter(Base):
    """一条指标范围条件：field op value（value 形态由 op 决定）。"""

    __tablename__ = "metric_filters"
    __table_args__ = (
        Index("ix_metric_filters_metric", "metric_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    metric_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("metric_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(String(200), nullable=False)
    # 与 query_engine.ALLOWED_OPERATORS 一致：eq/neq/gt/gte/lt/lte/in/between/like
    op: Mapped[str] = mapped_column(String(20), nullable=False)
    # in -> [v1, v2...]；between -> [lo, hi]；其余标量
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, nullable=False
    )
