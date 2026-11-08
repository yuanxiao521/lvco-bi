import logging
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_report_service
from app.core.database import get_db
from app.models.report import ReportStatus
from app.models.user import User
from app.schemas import CamelModel, SuccessResponse
from app.services.chart_export import build_option, render_options_to_svg_uris
from app.services.pdf_export import render_report_html
from app.services.report_service import ReportService
from app.services.storage_service import storage

router = APIRouter(prefix="/reports", tags=["报表"])

logger = logging.getLogger("lvco.reports")


class ReportCreateBody(CamelModel):
    title: str = Field(..., max_length=200)
    source_type: str = "manual"
    source_id: UUID | None = None
    snapshot_blocks: dict | None = None


class ReportStatusBody(CamelModel):
    status: str


class ReportUpdateBody(CamelModel):
    title: str = Field(..., max_length=200)


class ShareLinkBody(CamelModel):
    """分享链接期限参数（语义与 dashboards.ShareLinkBody 逐字一致）。

    expiresInDays：不传 → 默认 30 天；传 null 或 0 → 永久；传 N>0 → now + N 天。
    """

    expires_in_days: int | None = Field(default=None, ge=0, le=3650)


SHARE_DEFAULT_DAYS = 30


def _requested_share_days(body: "ShareLinkBody | None") -> int:
    """把请求体解析成"有效天数"（0 = 永久）；字段不传 = 默认 30 天。"""
    if body is None or "expires_in_days" not in body.model_fields_set:
        return SHARE_DEFAULT_DAYS
    if body.expires_in_days is None:
        return 0
    return body.expires_in_days


def _resolve_share_expiry(days: int) -> datetime | None:
    """天数 → 绝对过期时间；0 表示永久有效（落 NULL）。"""
    if days == 0:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


def _summary(r) -> dict:
    return {
        "id": str(r.id),
        "userId": str(r.user_id),
        "title": r.title,
        "sourceType": r.source_type.value if r.source_type else None,
        "sourceId": str(r.source_id) if r.source_id else None,
        "status": r.status.value if r.status else None,
        "shareToken": r.share_token,
        # 分享链接过期时间：从库里读（NULL = 永久有效或未分享）；不再由接口现编
        "shareExpiresAt": (
            r.share_expires_at.isoformat()
            if isinstance(getattr(r, "share_expires_at", None), datetime)
            else None
        ),
        "createdAt": r.created_at.isoformat() if r.created_at else None,
        "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("")
async def list_reports(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source_type: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    items, total = await service.list_reports(
        current_user.id, page, page_size, source_type, status_filter
    )
    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(
        data={
            "items": [_summary(item) for item in items],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "pages": pages,
        }
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_report(
    body: ReportCreateBody,
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        report = await service.create(
            user_id=current_user.id,
            title=body.title,
            source_type=body.source_type,
            source_id=body.source_id,
            snapshot_blocks=body.snapshot_blocks,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_REQUEST", "message": str(e)},
        )
    return SuccessResponse(data=_summary(report))


@router.get("/{report_id}")
async def get_report(
    report_id: UUID,
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    report = await service.get_by_id(report_id, current_user.id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )

    data = _summary(report)
    data["snapshotBlocks"] = report.snapshot_blocks
    return SuccessResponse(data=data)


@router.patch("/{report_id}")
async def update_report(
    report_id: UUID,
    body: ReportUpdateBody,
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    report = await service.update_title(report_id, current_user.id, body.title)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )
    return SuccessResponse(data=_summary(report))


@router.patch("/{report_id}/status")
async def update_report_status(
    report_id: UUID,
    body: ReportStatusBody,
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        report = await service.update_status(report_id, current_user.id, body.status)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_REQUEST", "message": str(e)},
        )
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )
    return SuccessResponse(data=_summary(report))


@router.post("/{report_id}/share")
async def share_report(
    report_id: UUID,
    body: ShareLinkBody | None = None,
    service: ReportService = Depends(get_report_service),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """生成 / 续期报表分享链接（仅属主）。

    过期时间真的写进 reports.share_expires_at，返回的 expiresAt 是 commit 后 refresh 读回的值。
    """
    report = await service.share(report_id, current_user.id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )

    expires_in_days = _requested_share_days(body)
    report.share_expires_at = _resolve_share_expiry(expires_in_days)
    await db.flush()
    await db.commit()
    await db.refresh(report)

    return SuccessResponse(
        data={
            "shareToken": report.share_token,
            "shareUrl": f"/share/report/{report.share_token}",
            # 库里读回；None = 永久有效
            "expiresAt": (
                report.share_expires_at.isoformat()
                if report.share_expires_at
                else None
            ),
            "permanent": report.share_expires_at is None,
            "expiresInDays": expires_in_days,
        }
    )


@router.delete("/{report_id}/share")
async def revoke_report_share(
    report_id: UUID,
    service: ReportService = Depends(get_report_service),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """撤销报表分享：清空 token 与过期时间，链接立即失效（仅属主）。

    非属主统一 404（不用 403）。若报表状态此前因分享被置为 shared，一并回落到 published，
    否则列表里会一直显示"已分享"而实际上没有可用链接。
    """
    report = await service.get_by_id(report_id, current_user.id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )

    report.share_token = None
    report.share_expires_at = None
    if getattr(report, "status", None) == ReportStatus.shared:
        report.status = ReportStatus.published
    await db.flush()
    await db.commit()

    return SuccessResponse(
        data={
            "reportId": str(report_id),
            "revoked": True,
            "shareToken": None,
            "expiresAt": None,
        }
    )


@router.get("/{report_id}/export/pdf")
async def export_report_pdf(
    report_id: UUID,
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
):
    report = await service.get_by_id(report_id, current_user.id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )

    snapshot = report.snapshot_blocks
    if not isinstance(snapshot, dict):
        snapshot = {}
    blocks = snapshot.get("blocks") if isinstance(snapshot.get("blocks"), list) else []

    # Process blocks: convert chart blocks to images
    from app.services.chart_renderer import render_chart
    processed_blocks = []
    # 待批量渲染成 SVG 的图表块（与前端画布同一份 ECharts option），渲染完覆盖 matplotlib 结果
    pending_svgs: list[tuple[dict, dict]] = []
    for block in blocks:
        block_copy = dict(block) if isinstance(block, dict) else block
        btype = str(block_copy.get("type", block_copy.get("blockType", ""))).strip().lower()
        if btype == "chart":
            title = block_copy.get("title", "图表")
            chart_type = (
                block_copy.get("chartType")
                or (block_copy.get("_chartResult") or {}).get("chartType")
                or block_copy.get("renderer")
                or "bar"
            )
            # 如果 renderer 是 echarts/recharts，chart_type 应优先看 _chartResult.chartType
            renderer_field = block_copy.get("renderer")
            if renderer_field in ("echarts", "recharts") and isinstance(block_copy.get("_chartResult"), dict):
                chart_type = block_copy["_chartResult"].get("chartType") or chart_type

            chart_images: list[str] = []
            chart_result = block_copy.get("_chartResult")
            chart_result_used = False

            # 0. 首选：复用 render_chart 的 ECharts option 服务端渲染 SVG（与前端同一引擎/配色）
            if isinstance(chart_result, dict) and chart_result.get("columns") and chart_result.get("rows"):
                option = await build_option(
                    chart_type, title, chart_result["columns"], chart_result["rows"]
                )
                if option:
                    pending_svgs.append((block_copy, option))

            # 1. 优先取 _chartResult 的真实查询结果（前端自动保存嵌入）
            if isinstance(chart_result, dict) and chart_result.get("rows"):
                rows = chart_result["rows"]
                columns = chart_result.get("columns", [])
                if len(columns) >= 2 and rows:
                    x_col = columns[0]
                    value_cols = columns[1:]
                    x_labels = [str(r.get(x_col, "")) if r.get(x_col) is not None else "" for r in rows]
                    for vcol in value_cols:
                        values = []
                        for r in rows:
                            v = r.get(vcol, 0)
                            try:
                                values.append(float(v) if v is not None else 0.0)
                            except (ValueError, TypeError):
                                values.append(0.0)
                        if x_labels and values:
                            try:
                                img = render_chart(chart_type, f"{title} - {vcol}", x_labels, values)
                                chart_images.append(img)
                            except Exception:
                                pass
                    if chart_images:
                        chart_result_used = True

            # 2. 兜底：旧数据格式 (chart_data.labels/values)
            if not chart_result_used:
                chart_data = block_copy.get("data", block_copy.get("chartData", {}))
                labels = []
                values = []
                if isinstance(chart_data, dict):
                    labels = chart_data.get("labels", []) or chart_data.get("xAxis", []) or []
                    values = chart_data.get("values", []) or chart_data.get("series", []) or []
                if isinstance(values, list) and values and isinstance(values[0], dict):
                    values = values[0].get("data", []) if values else []
                if labels and values:
                    try:
                        chart_images.append(render_chart(chart_type, title, labels, values))
                    except Exception:
                        pass

            if chart_images:
                # 同时保留 _chart_image (兼容 render_report_html) 和 _chart_images (canvas 模板)
                block_copy["_chart_image"] = chart_images[0]
                block_copy["_chart_images"] = chart_images
                block_copy["_resolved_chart_type"] = chart_type
        processed_blocks.append(block_copy)

    # 批量渲染 SVG（一次浏览器渲染全部图），成功则覆盖该块的 matplotlib PNG
    if pending_svgs:
        svg_uris = await render_options_to_svg_uris([opt for _, opt in pending_svgs])
        for (blk, _), uri in zip(pending_svgs, svg_uris):
            if uri:
                blk["_chart_image"] = uri
                blk["_chart_images"] = [uri]
        logger.info("report pdf svg rendered %d/%d", sum(1 for u in svg_uris if u), len(svg_uris))

    html = render_report_html(
        title=report.title,
        status=report.status.value if report.status else "draft",
        snapshot_blocks=processed_blocks,
    )

    # Generate PDF using Playwright subprocess
    from app.api.v1.canvases import _html_to_pdf_subprocess
    try:
        pdf_bytes = await _html_to_pdf_subprocess(html)
    except Exception as e:
        logger.warning("Report PDF generation failed: %s", e)
        # Fallback: return HTML directly with .html extension
        from urllib.parse import quote as _quote
        safe_title_ascii_html = "report.html"
        safe_title_utf8_html = _quote((report.title or "report") + ".html", safe="")
        return HTMLResponse(
            content=html,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{safe_title_ascii_html}"; '
                    f"filename*=UTF-8''{safe_title_utf8_html}"
                )
            },
        )

    # Try MinIO upload for PDF
    minio_key = f"reports/{report_id}/export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    result = storage.put_object("lvco-reports", minio_key, pdf_bytes, "application/pdf")
    if result:
        presigned = storage.get_presigned_url("lvco-reports", minio_key)
        if presigned:
            return JSONResponse(
                content={"url": presigned, "message": "报表导出成功"},
            )

    # Fallback: return PDF directly
    # 注意：HTTP header 默认 latin-1，含中文文件名必须用 RFC 5987 的 filename* 编码
    from urllib.parse import quote
    safe_title_ascii = "report.pdf"
    safe_title_utf8 = quote((report.title or "report") + ".pdf", safe="")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{safe_title_ascii}\"; "
                f"filename*=UTF-8''{safe_title_utf8}"
            )
        },
    )


@router.delete("/{report_id}")
async def delete_report(
    report_id: UUID,
    service: ReportService = Depends(get_report_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    deleted = await service.delete(report_id, current_user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "报表不存在"},
        )
    return SuccessResponse(data={"message": "已删除"})