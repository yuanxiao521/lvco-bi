"""报告仓库实现

基于 SQLAlchemy 实现 ReportRepository 协议
"""
import logging
import secrets
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.report import Report, ReportSourceType, ReportStatus
from app.repositories.protocols import ReportRepository

logger = logging.getLogger(__name__)


_ALLOWED_STATUS = {"draft", "published", "shared", "deleted"}


def _make_share_token() -> str:
    """生成报告分享 token"""
    return f"shr_report_{secrets.token_urlsafe(8)}"


def _normalize_status(raw: str) -> ReportStatus:
    """归一化状态字符串"""
    s = raw.lower()
    if s not in _ALLOWED_STATUS:
        raise ValueError(f"非法状态: {raw}，仅支持 draft/published/shared/deleted")
    return ReportStatus(s)


class SQLAlchemyReportRepository(ReportRepository):
    """基于 SQLAlchemy 的报告仓库实现"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(
        self,
        user_id: UUID,
        title: str,
        source_type: str,
        source_id: UUID | None,
        snapshot_blocks: dict | None,
    ) -> Report:
        """创建报告"""
        logger.debug(
            f"create: user_id={user_id}, title={title}, source_type={source_type}"
        )

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

        logger.debug(f"create: created, id={report.id}")

        return report

    async def list_reports(
        self,
        user_id: UUID,
        page: int,
        page_size: int,
        source_type: str | None,
        status_filter: str | None,
    ) -> tuple[list[Report], int]:
        """查询报告列表（分页）"""
        logger.debug(
            f"list_reports: user_id={user_id}, page={page}, page_size={page_size}, "
            f"source_type={source_type}, status_filter={status_filter}"
        )

        # 先把外部传入的 status 字符串归一化
        normalized_status: ReportStatus | None = None
        if status_filter:
            try:
                normalized_status = _normalize_status(status_filter)
            except ValueError:
                pass

        q = select(Report).where(Report.user_id == user_id)
        count_q = select(func.count()).select_from(Report).where(Report.user_id == user_id)

        # Exclude soft-deleted reports unless explicitly filtered
        if normalized_status == ReportStatus.deleted:
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

        if normalized_status is not None:
            q = q.where(Report.status == normalized_status)
            count_q = count_q.where(Report.status == normalized_status)

        total = (await self.db.execute(count_q)).scalar() or 0

        q = (
            q.order_by(Report.updated_at.desc().nullslast(), Report.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(q)
        items = list(result.scalars().all())

        logger.debug(f"list_reports: found {len(items)} reports, total={total}")

        return items, total

    async def get_by_id(self, report_id: UUID, user_id: UUID) -> Report | None:
        """根据 ID 查询报告"""
        logger.debug(f"get_by_id: report_id={report_id}, user_id={user_id}")

        result = await self.db.execute(
            select(Report).where(Report.id == report_id, Report.user_id == user_id)
        )
        report = result.scalar_one_or_none()

        if report:
            logger.debug(f"get_by_id: found, title={report.title}")
        else:
            logger.debug(f"get_by_id: not found")

        return report

    async def update_status(self, report: Report, status: str) -> Report:
        """更新报告状态"""
        logger.debug(f"update_status: id={report.id}, status={status}")

        report.status = _normalize_status(status)
        await self.db.flush()
        await self.db.refresh(report)

        logger.debug(f"update_status: updated, status={report.status}")

        return report

    async def update_title(self, report: Report, title: str) -> Report:
        """更新报告标题"""
        logger.debug(f"update_title: id={report.id}, title={title}")

        report.title = title
        await self.db.flush()
        await self.db.refresh(report)

        logger.debug(f"update_title: updated")

        return report

    async def share(self, report: Report) -> Report:
        """生成报告分享链接"""
        logger.debug(f"share: id={report.id}")

        if report.share_token is None:
            report.share_token = _make_share_token()
        report.status = ReportStatus.shared
        await self.db.flush()
        await self.db.refresh(report)

        logger.debug(f"share: generated, token={report.share_token}")

        return report

    async def delete(self, report: Report) -> None:
        """软删除报告"""
        logger.debug(f"delete: id={report.id}")

        report.status = ReportStatus.deleted
        await self.db.flush()

        logger.debug("delete: deleted")
