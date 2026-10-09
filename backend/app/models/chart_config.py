import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChartType(str, enum.Enum):
    bar = "bar"
    line = "line"
    pie = "pie"
    scatter = "scatter"
    area = "area"
    donut = "donut"
    # Phase 4 扩展：转化漏斗 / 交叉分析 / 多维评分 / 流量路径
    funnel = "funnel"
    heatmap = "heatmap"
    radar = "radar"
    sankey = "sankey"
    # 多度量 / KPI 变体
    grouped_bar = "grouped_bar"
    stacked_bar = "stacked_bar"
    kpi_card = "kpi_card"
    # 水平条形图（横向 bar，便于长类别名 / 排名场景）
    horizontal_bar = "horizontal_bar"


class ChartConfig(Base):
    __tablename__ = "chart_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    datasource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    chart_type: Mapped[ChartType] = mapped_column(
        Enum(ChartType, name="chart_type"), nullable=False
    )
    query_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    render_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
