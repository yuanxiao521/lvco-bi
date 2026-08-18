import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import Report, ReportSourceType, ReportStatus


_ALLOWED_STATUS = {"draft", "published", "shared", "archived", "deleted"}


def _make_share_token() -> str:
    return f"shr_report_{secrets.token_urlsafe(8)}"


def _normalize_status(raw: str) -> ReportStatus:
    s = raw.lower()
    if s not in _ALLOWED_STATUS:
        raise ValueError(f"非法状态: {raw}，仅支持 draft/published/shared/archived")
    return ReportStatus(s)


class ReportService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        user_id: UUID,
        title: str,
        source_type: str,
        source_id: UUID | None = None,
        snapshot_blocks: dict | None = None,
    ) -> Report:
        report = Report(
            user_id=user_id,
            title=title,
            source_type=ReportSourceType(source_type),
            source_id=source_id,
            snapshot_blocks=snapshot_blocks or {},
            status=ReportStatus.draft,
        )
        self.db.add(report)
        await self.db.flush()
        await self.db.refresh(report)
        return report

    async def list_reports(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        source_type: str | None = None,
        status_filter: str | None = None,
    ) -> tuple[list[Report], int]:
        q = select(Report).where(Report.user_id == user_id)
        count_q = select(func.count()).select_from(Report).where(Report.user_id == user_id)

        # Exclude soft-deleted reports unless explicitly filtered
        if status_filter and _normalize_status(status_filter) == ReportStatus.deleted:
            pass  # user explicitly asked for deleted
        else:
            q = q.where(Report.status != ReportStatus.deleted)
            count_q = count_q.where(Report.status != ReportStatus.deleted)

        if source_type:
            try:
                st = ReportSourceType(source_type)
                q = q.where(Report.source_type == st)
                count_q = count_q.where(Report.source_type == st)
            except ValueError:
                pass

        if status_filter:
            try:
                rs = _normalize_status(status_filter)
                q = q.where(Report.status == rs)
                count_q = count_q.where(Report.status == rs)
            except ValueError:
                pass

        total = (await self.db.execute(count_q)).scalar() or 0

        q = (
            q.order_by(Report.updated_at.desc().nullslast(), Report.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(q)
        items = list(result.scalars().all())
        return items, total

    async def get_by_id(self, report_id: UUID, user_id: UUID) -> Report | None:
        result = await self.db.execute(
            select(Report).where(Report.id == report_id, Report.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def update_status(self, report_id: UUID, user_id: UUID, status: str) -> Report | None:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return None
        report.status = _normalize_status(status)
        await self.db.flush()
        await self.db.refresh(report)
        return report

    async def update_title(self, report_id: UUID, user_id: UUID, title: str) -> Report | None:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return None
        report.title = title
        await self.db.flush()
        await self.db.refresh(report)
        return report

    async def share(self, report_id: UUID, user_id: UUID) -> Report | None:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return None
        if report.share_token is None:
            report.share_token = _make_share_token()
        report.status = ReportStatus.shared
        await self.db.flush()
        await self.db.refresh(report)
        return report

    async def delete(self, report_id: UUID, user_id: UUID) -> bool:
        report = await self.get_by_id(report_id, user_id)
        if report is None:
            return False
        # Soft delete: set status to deleted
        report.status = ReportStatus.deleted
        await self.db.flush()
        return True


def _calc_pages(total: int, page_size: int) -> int:
    return math.ceil(total / page_size) if total > 0 else 0