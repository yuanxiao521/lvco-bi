import logging
import math
from typing import Any
from uuid import UUID

from app.models.canvas import Canvas
from app.models.chart_config import ChartConfig, ChartType
from app.repositories.protocols import CanvasRepository, ChartConfigRepository

logger = logging.getLogger(__name__)


class CanvasService:
    """画布服务层。
    
    通过 Repository 模式访问数据，不直接操作数据库。
    """
    
    def __init__(
        self,
        canvas_repo: CanvasRepository,
        chart_config_repo: ChartConfigRepository,
    ) -> None:
        self.canvas_repo = canvas_repo
        self.chart_config_repo = chart_config_repo

    async def create(
        self,
        user_id: UUID,
        title: str,
        datasource_id: UUID | None,
        table_name: str | None = None,
    ) -> Canvas:
        """创建画布。"""
        logger.info(f"创建画布 user_id={user_id} title={title} datasource_id={datasource_id}")
        canvas = await self.canvas_repo.create(
            user_id=user_id,
            title=title,
            datasource_id=datasource_id,
            table_name=table_name,
        )
        logger.info(f"画布创建成功 canvas_id={canvas.id}")
        return canvas

    async def create_chart_config(
        self,
        chart_type: ChartType,
        query_config: dict,
        datasource_id: UUID | None = None,
        render_config: dict | None = None,
    ) -> ChartConfig:
        """创建图表配置。"""
        logger.info(f"创建图表配置 chart_type={chart_type} datasource_id={datasource_id}")
        cc = await self.chart_config_repo.create(
            chart_type=chart_type,
            query_config=query_config,
            datasource_id=datasource_id,
            render_config=render_config,
        )
        logger.info(f"图表配置创建成功 chart_config_id={cc.id}")
        return cc

    async def list_canvases(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
    ) -> tuple[list[Canvas], int]:
        """查询画布列表（分页）。"""
        logger.debug(f"查询画布列表 user_id={user_id} page={page} page_size={page_size}")
        items, total = await self.canvas_repo.list_canvases(
            user_id=user_id,
            page=page,
            page_size=page_size,
        )
        logger.debug(f"画布列表查询完成 total={total} returned={len(items)}")
        return items, total

    async def get_by_id(self, canvas_id: UUID, user_id: UUID) -> Canvas | None:
        """根据 ID 查询画布。"""
        logger.debug(f"查询画布 canvas_id={canvas_id} user_id={user_id}")
        canvas = await self.canvas_repo.get_by_id(canvas_id, user_id)
        if canvas:
            logger.debug(f"画布查询成功 canvas_id={canvas_id} title={canvas.title}")
        else:
            logger.debug(f"画布未找到 canvas_id={canvas_id}")
        return canvas

    async def update_blocks(self, canvas_id: UUID, user_id: UUID, blocks: list[Any]) -> Canvas | None:
        """更新画布的 blocks。"""
        logger.info(f"更新画布块 canvas_id={canvas_id} user_id={user_id} blocks_count={len(blocks)}")
        canvas = await self.canvas_repo.update_blocks(canvas_id, user_id, blocks)
        if canvas is None:
            logger.warning(f"更新画布块失败：画布不存在 canvas_id={canvas_id}")
        else:
            logger.info(f"画布块更新成功 canvas_id={canvas_id}")
        return canvas

    async def delete(self, canvas_id: UUID, user_id: UUID) -> bool:
        """软删除画布。"""
        logger.info(f"删除画布 canvas_id={canvas_id} user_id={user_id}")
        success = await self.canvas_repo.delete(canvas_id, user_id)
        if success:
            logger.info(f"画布删除成功 canvas_id={canvas_id}")
        else:
            logger.warning(f"删除画布失败：画布不存在 canvas_id={canvas_id}")
        return success

    async def update_title(self, canvas_id: UUID, user_id: UUID, title: str) -> Canvas | None:
        """更新画布标题。"""
        logger.info(f"更新画布标题 canvas_id={canvas_id} new_title={title}")
        canvas = await self.canvas_repo.update_title(canvas_id, user_id, title)
        if canvas is None:
            logger.warning(f"更新画布标题失败：画布不存在 canvas_id={canvas_id}")
        else:
            logger.info(f"画布标题更新成功 canvas_id={canvas_id}")
        return canvas

    @staticmethod
    def calc_pages(total: int, page_size: int) -> int:
        """计算总页数。"""
        return math.ceil(total / page_size) if total > 0 else 0