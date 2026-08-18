import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Canvas
from app.models.chart_config import ChartConfig, ChartType


class CanvasService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id: UUID,
        title: str,
        datasource_id: UUID | None,
        table_name: str | None = None,
    ) -> Canvas:
        canvas = Canvas(
            user_id=user_id,
            title=title,
            datasource_id=datasource_id,
            table_name=table_name,
            blocks=[],
        )
        self.db.add(canvas)
        await self.db.flush()
        await self.db.refresh(canvas)
        return canvas

    async def create_chart_config(
        self,
        chart_type: ChartType,
        query_config: dict,
        datasource_id: UUID | None = None,
        render_config: dict | None = None,
    ) -> ChartConfig:
        cc = ChartConfig(
            chart_type=chart_type,
            query_config=query_config,
            datasource_id=datasource_id,
            render_config=render_config,
        )
        self.db.add(cc)
        await self.db.flush()
        await self.db.refresh(cc)
        return cc

    async def list_canvases(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[Canvas], int]:
        base_where = (Canvas.user_id == user_id) & (Canvas.deleted_at.is_(None))
        count_q = select(func.count()).select_from(Canvas).where(base_where)
        total = (await self.db.execute(count_q)).scalar() or 0

        q = (
            select(Canvas)
            .where(base_where)
            .order_by(Canvas.updated_at.desc().nullslast(), Canvas.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(q)
        items = list(result.scalars().all())
        return items, total

    async def get_by_id(self, canvas_id: UUID, user_id: UUID) -> Canvas | None:
        result = await self.db.execute(
            select(Canvas).where(Canvas.id == canvas_id, Canvas.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_blocks(self, canvas_id: UUID, user_id: UUID, blocks: list[Any]) -> Canvas | None:
        canvas = await self.get_by_id(canvas_id, user_id)
        if canvas is None:
            return None
        canvas.blocks = blocks
        await self.db.flush()
        await self.db.refresh(canvas)
        return canvas

    async def delete(self, canvas_id: UUID, user_id: UUID) -> bool:
        canvas = await self.get_by_id(canvas_id, user_id)
        if canvas is None:
            return False
        # Soft delete
        canvas.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        return True

    async def update_title(self, canvas_id: UUID, user_id: UUID, title: str) -> Canvas | None:
        canvas = await self.get_by_id(canvas_id, user_id)
        if canvas is None:
            return None
        canvas.title = title
        await self.db.flush()
        await self.db.refresh(canvas)
        return canvas

    @staticmethod
    def calc_pages(total: int, page_size: int) -> int:
        return math.ceil(total / page_size) if total > 0 else 0