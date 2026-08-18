"""Canvas Repository 实现。

封装画布相关的数据库操作，实现 CanvasRepository 协议。
"""
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canvas import Canvas
from app.models.chart_config import ChartConfig, ChartType

logger = logging.getLogger(__name__)


class SQLAlchemyCanvasRepository:
    """基于 SQLAlchemy 的画布仓库实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id: UUID,
        title: str,
        datasource_id: UUID | None,
        table_name: str | None = None,
    ) -> Canvas:
        """创建画布。"""
        logger.info(f"创建画布 user_id={user_id} title={title} datasource_id={datasource_id}")
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
        logger.info(f"画布创建成功 canvas_id={canvas.id}")
        return canvas

    async def list_canvases(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[Canvas], int]:
        """查询画布列表（分页）。"""
        logger.debug(f"查询画布列表 user_id={user_id} page={page} page_size={page_size}")
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
        logger.debug(f"画布列表查询完成 total={total} returned={len(items)}")
        return items, total

    async def get_by_id(self, canvas_id: UUID, user_id: UUID) -> Canvas | None:
        """根据 ID 查询画布。"""
        logger.debug(f"查询画布 canvas_id={canvas_id} user_id={user_id}")
        result = await self.db.execute(
            select(Canvas).where(Canvas.id == canvas_id, Canvas.user_id == user_id)
        )
        canvas = result.scalar_one_or_none()
        if canvas:
            logger.debug(f"画布查询成功 canvas_id={canvas_id} title={canvas.title}")
        else:
            logger.debug(f"画布未找到 canvas_id={canvas_id}")
        return canvas

    async def update_blocks(self, canvas_id: UUID, user_id: UUID, blocks: list[Any]) -> Canvas | None:
        """更新画布的 blocks。"""
        logger.info(f"更新画布块 canvas_id={canvas_id} user_id={user_id} blocks_count={len(blocks)}")
        canvas = await self.get_by_id(canvas_id, user_id)
        if canvas is None:
            logger.warning(f"更新画布块失败：画布不存在 canvas_id={canvas_id}")
            return None
        canvas.blocks = blocks
        await self.db.flush()
        await self.db.refresh(canvas)
        logger.info(f"画布块更新成功 canvas_id={canvas_id}")
        return canvas

    async def delete(self, canvas_id: UUID, user_id: UUID) -> bool:
        """软删除画布。"""
        logger.info(f"删除画布 canvas_id={canvas_id} user_id={user_id}")
        canvas = await self.get_by_id(canvas_id, user_id)
        if canvas is None:
            logger.warning(f"删除画布失败：画布不存在 canvas_id={canvas_id}")
            return False
        # Soft delete
        canvas.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        logger.info(f"画布删除成功 canvas_id={canvas_id}")
        return True

    async def update_title(self, canvas_id: UUID, user_id: UUID, title: str) -> Canvas | None:
        """更新画布标题。"""
        logger.info(f"更新画布标题 canvas_id={canvas_id} new_title={title}")
        canvas = await self.get_by_id(canvas_id, user_id)
        if canvas is None:
            logger.warning(f"更新画布标题失败：画布不存在 canvas_id={canvas_id}")
            return None
        canvas.title = title
        await self.db.flush()
        await self.db.refresh(canvas)
        logger.info(f"画布标题更新成功 canvas_id={canvas_id}")
        return canvas


class SQLAlchemyChartConfigRepository:
    """基于 SQLAlchemy 的图表配置仓库实现。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        chart_type: ChartType,
        query_config: dict,
        datasource_id: UUID | None = None,
        render_config: dict | None = None,
    ) -> ChartConfig:
        """创建图表配置。"""
        logger.info(f"创建图表配置 chart_type={chart_type} datasource_id={datasource_id}")
        cc = ChartConfig(
            chart_type=chart_type,
            query_config=query_config,
            datasource_id=datasource_id,
            render_config=render_config,
        )
        self.db.add(cc)
        await self.db.flush()
        await self.db.refresh(cc)
        logger.info(f"图表配置创建成功 chart_config_id={cc.id}")
        return cc
