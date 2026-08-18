import math
from typing import Any
from uuid import UUID

from app.models.report import Report
from app.repositories.protocols import ReportRepository


class ReportService:
    def __init__(self, report_repo: ReportRepository) -> None:
        self.report_repo = report_repo

    async def create(
        self,
        user_id: UUID,
        title: str,
        source_type: str,
        source_id: UUID | None = None,
        snapshot_blocks: dict | None = None,
    ) -> Report:
        return await self.report_repo.create(
            user_id=user_id,
            title=title,
            source_type=source_type,
            source_id=source_id,
            snapshot_blocks=snapshot_blocks,
        )

    async def list_reports(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        source_type: str | None = None,
        status_filter: str | None = None,
    ) -> tuple[list[Report], int]:
        return await self.report_repo.list_reports(
            user_id=user_id,
            page=page,
            page_size=page_size,
            source_type=source_type,
            status_filter=status_filter,
        )

    async def get_by_id(self, report_id: UUID, user_id: UUID) -> Report | None:
        return await self.report_repo.get_by_id(report_id, user_id)

    async def update_status(self, report_id: UUID, user_id: UUID, status: str) -> Report | None:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return None
        return await self.report_repo.update_status(report, status)

    async def update_title(self, report_id: UUID, user_id: UUID, title: str) -> Report | None:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return None
        return await self.report_repo.update_title(report, title)

    async def share(self, report_id: UUID, user_id: UUID) -> Report | None:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return None
        return await self.report_repo.share(report)

    async def delete(self, report_id: UUID, user_id: UUID) -> bool:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return False
        await self.report_repo.delete(report)
        return True


def _calc_pages(total: int, page_size: int) -> int:
    return math.ceil(total / page_size) if total > 0 else 0