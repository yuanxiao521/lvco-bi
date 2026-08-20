import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MetricDefinition(TimestampMixin, Base):
    """指标定义（指标语义层核心实体）。

    一次定义、处处引用：把"销售额 = SUM(amount)"这类口径沉淀为命名指标，
    供画布块 / 图表配置 / AI 问答通过 metric_id 引用，而非重复写 {field, agg}。

    口径核心是 formula（聚合 SQL 表达式）：
    - "销售额"      -> SUM("amount")
    - "订单量"      -> COUNT("order_id")
    - "复购率"      -> COUNT(DISTINCT "customer_id") (维度由查询时按场景补充)
    - "会员总数"    -> COUNT(DISTINCT "member_id")
    - "客单价"      -> SUM("amount") / （不可聚合的除法由上层复合，单指标先取 SUM）
    具体公式语义由 description 明确（含税/不含税、是否去重等口径说明）。
    """

    __tablename__ = "metric_definitions"
    __table_args__ = (
        UniqueConstraint("key", "user_id", name="uq_metrics_key_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # 英文键，供 AI / 检索 / formula 别名使用，如 sales_amount
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    # 中文展示名，如 销售额
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 口径说明（含税/不含税、是否去重、统计范围等），AI 可读
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # 口径核心：聚合 SQL 表达式，如 SUM("amount") / COUNT(DISTINCT "customer_id")
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    # 辅助聚合标记（SUM/COUNT/AVG/…），供 schema 提示与校验
    agg_kind: Mapped[str | None] = mapped_column(String(20), nullable=True, default="SUM")
    # 指标归属的数据源；为空表示"通用模板指标"（可绑定到任意同结构数据源）
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 指标引用的表名（缺省 data）
    table_ref: Mapped[str | None] = mapped_column(String(200), nullable=True, default="data")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )