import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import traceback
import uuid as _uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from jinja2 import Template
from playwright.async_api import async_playwright
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_canvas_service, get_current_user, get_dashboard_service
from app.config import settings
from app.core.database import get_db
from app.models.chart_config import ChartType
from app.models.datasource import DataSource
from app.models.report import Report, ReportSourceType, ReportStatus
from app.models.user import User
from app.schemas import CamelModel, CanvasResponse, SuccessResponse
from app.schemas.query import ChartQueryConfig
from app.services.ai_service import AIService
from app.services.canvas_service import CanvasService
from app.services.chart_renderer import render_chart
from app.services.dashboard_service import DashboardService
from app.services.llm_client import AINotConfiguredError, LLMClient
from app.services.query_engine import QueryEngineError, execute_chart_query

router = APIRouter(prefix="/canvases", tags=["画布"])

logger = logging.getLogger("lvco.canvases")

import base64 as _base64_module
import tempfile


def _image_to_base64_data_uri(src: str) -> str | None:
    """Convert an image URL or local path to a data URI. Returns None on failure."""
    try:
        if src.startswith("data:"):
            return src  # already a data URI
        parsed = __import__("urllib.parse").urlparse(src)
        if parsed.scheme in ("http", "https"):
            import requests as _requests
            resp = _requests.get(src, timeout=10)
            resp.raise_for_status()
            content = resp.content
        elif not parsed.scheme or parsed.scheme == "file":
            path = src
            if path.startswith("file://"):
                path = __import__("urllib.request").url2pathname(path[7:])
            with open(path, "rb") as f:
                content = f.read()
        else:
            return None
        ext = (src.rsplit("?", 1)[0]).rsplit(".", 1)[-1].lower()
        mime = f"image/{ext}" if ext in ("png", "jpeg", "jpg", "gif", "webp", "svg") else "image/png"
        return f"data:{mime};base64,{_base64_module.b64encode(content).decode()}"
    except Exception:
        return None


async def _html_to_pdf_subprocess(html: str) -> bytes:
    """通过子进程调用 Playwright，避免 Windows ProactorEventLoop 限制。"""
    import subprocess as _subprocess

    # __file__ = .../backend/app/api/v1/canvases.py → 向上4层到 backend/
    fdir = os.path.dirname(os.path.abspath(__file__))
    backend_root = os.path.dirname(os.path.dirname(os.path.dirname(fdir)))
    if not backend_root.endswith("backend"):
        backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    script_path = os.path.join(backend_root, "scripts", "pdf_worker.py")
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"pdf_worker.py not found at {script_path}")

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as html_f:
        html_f.write(html)
        html_path = html_f.name

    pdf_path = html_path.replace(".html", ".pdf")
    try:
        # 使用同步 subprocess.run() 而非 asyncio.create_subprocess_exec()
        # 避免 Windows ProactorEventLoop 子进程传输问题
        proc = await asyncio.to_thread(
            _subprocess.run,
            [sys.executable, script_path, html_path, pdf_path],
            capture_output=True, text=True, timeout=120,
        )

        if proc.returncode != 0:
            raise RuntimeError(f"PDF worker failed (rc={proc.returncode}): {proc.stderr[:500]}")

        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for p in (html_path, pdf_path):
            try:
                os.unlink(p)
            except (FileNotFoundError, PermissionError):
                pass


_CANVAS_PDF_HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>{{ canvas.title }}</title>
<style>
body { font-family: "Microsoft YaHei", "SimHei", "PingFang SC", "Segoe UI", sans-serif; margin: 32px; color: #222; }
h1, h2 { color: #111; }
.block-title-h1 { font-size: 28px; margin: 24px 0 12px; border-bottom: 2px solid #2BB5A0; padding-bottom: 8px; }
.block-title-h2 { font-size: 20px; margin: 20px 0 10px; }
.block-text { font-size: 14px; line-height: 1.7; margin: 8px 0; white-space: pre-wrap; }
.block-chart { margin: 16px 0; text-align: center; }
.block-chart img { max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.block-image { max-width: 100%; height: auto; display: block; margin: 12px auto; border-radius: 8px; }
.meta { color: #888; font-size: 12px; margin-bottom: 24px; }
</style>
</head>
<body>
<h1>{{ canvas.title }}</h1>
<p class="meta">画布 ID: {{ canvas.id }} | 导出时间: {{ exported_at }}</p>
{% for block in blocks %}
{% set btype = (block.get('type', block.get('blockType', ''))|string|trim|lower) %}
{% if btype == 'h1' %}
<h1 class="block-title-h1">{{ block.get('content', '') }}</h1>
{% elif btype == 'h2' %}
<h2 class="block-title-h2">{{ block.get('content', '') }}</h2>
{% elif btype == 'text' %}
<p class="block-text">{{ block.get('content', '') }}</p>
{% elif btype == 'chart' %}
<div class="block-chart">
{% if block.get('_chart_images') %}
{% for img in block['_chart_images'] %}
<img src="{{ img }}" alt="{{ block.get('title', '图表') }}" style="max-width:100%; height:auto; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.1); margin-bottom:8px;" />
{% endfor %}
{% elif block.get('_chart_image') %}
<img src="{{ block['_chart_image'] }}" alt="{{ block.get('title', '图表') }}" />
{% else %}
<p style="color:#999;">[图表: {{ block.get('title', '未命名') }}]</p>
{% endif %}
</div>
{% elif btype == 'image' %}
{% if block.get('_image_data') %}
<img class="block-image" src="{{ block['_image_data'] }}" alt="{{ block.get('alt', '') }}" />
{% elif block.get('src') %}
<img class="block-image" src="{{ block['src'] }}" alt="{{ block.get('alt', '') }}" />
{% endif %}
{% else %}
<div class="block-text">[未支持块类型: {{ btype }}]</div>
{% endif %}
{% endfor %}
</body>
</html>
"""
)


class CanvasCreateBody(CamelModel):
    title: str = Field(..., max_length=200)
    datasource_id: UUID
    table_name: str | None = None


class CanvasBlocksBody(CamelModel):
    blocks: list = Field(default_factory=list)


class CanvasUpdateBody(CamelModel):
    title: str = Field(..., max_length=200)


class CanvasChartConfigBody(CamelModel):
    chart_type: str
    query_config: dict
    block_id: str | None = None


class SaveAsReportBody(CamelModel):
    title: str = Field(..., max_length=200)
    description: str | None = None
    status: str = "draft"


class PinToDashboardBody(CamelModel):
    dashboard_id: UUID
    chart_config_id: UUID | None = None
    chart_config: dict | None = None
    position: dict = Field(default_factory=lambda: {"x": 0, "y": 0, "w": 6, "h": 4})


class AIRecommendCanvasRequest(CamelModel):
    current_config: dict
    datasource_id: UUID | None = None


class AIImageBody(CamelModel):
    description: str = Field(..., min_length=1)


@router.get("")
async def list_canvases(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    items, total = await service.list_canvases(current_user.id, page, page_size)
    pages = math.ceil(total / page_size) if total > 0 else 0
    return SuccessResponse(
        data={
            "items": [CanvasResponse.model_validate(item).model_dump(mode="json", by_alias=True) for item in items],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "pages": pages,
        }
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_canvas(
    body: CanvasCreateBody,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    canvas = await service.create(
        user_id=current_user.id,
        title=body.title,
        datasource_id=body.datasource_id,
        table_name=body.table_name,
    )
    return SuccessResponse(data=CanvasResponse.model_validate(canvas).model_dump(mode="json", by_alias=True))


@router.get("/{canvas_id}")
async def get_canvas(
    canvas_id: UUID,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    canvas = await service.get_by_id(canvas_id, current_user.id)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )
    return SuccessResponse(data=CanvasResponse.model_validate(canvas).model_dump(mode="json", by_alias=True))


@router.patch("/{canvas_id}")
async def update_canvas(
    canvas_id: UUID,
    body: CanvasUpdateBody,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    canvas = await service.update_title(canvas_id, current_user.id, body.title)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )
    return SuccessResponse(data=CanvasResponse.model_validate(canvas).model_dump(mode="json", by_alias=True))


@router.put("/{canvas_id}/blocks")
async def update_blocks(
    canvas_id: UUID,
    body: CanvasBlocksBody,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    canvas = await service.update_blocks(canvas_id, current_user.id, body.blocks)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )
    return SuccessResponse(data=CanvasResponse.model_validate(canvas).model_dump(mode="json", by_alias=True))


@router.post("/{canvas_id}/query")
async def query_canvas(
    canvas_id: UUID,
    body: ChartQueryConfig,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """画布图表查询。force=true 时跳过缓存读和缓存写（用户手动刷新用）。"""
    canvas = await service.get_by_id(canvas_id, current_user.id)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )

    # 优先使用请求中指定的数据源（支持一个画布多个数据源），fallback 到画布绑定的数据源
    query_datasource_id = canvas.datasource_id
    if body.datasource_id:
        try:
            ds_uuid = UUID(body.datasource_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_FIELD", "message": f"无效的数据源 ID: {body.datasource_id}"},
            )
        # 验证数据源存在且属于当前用户，不存在时回退到画布默认数据源
        ds_check = await db.execute(
            select(DataSource).where(
                DataSource.id == ds_uuid,
                DataSource.user_id == current_user.id,
            )
        )
        if ds_check.scalar_one_or_none() is not None:
            query_datasource_id = ds_uuid
        else:
            logger.warning(
                "query_canvas: 图表块 datasource_id=%s 不存在，回退到画布默认 %s",
                str(ds_uuid), str(canvas.datasource_id),
            )
    if query_datasource_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "MISSING_DATASOURCE", "message": "画布未绑定数据源，请在请求中指定 datasourceId"},
        )

    try:
        result = await execute_chart_query(
            datasource_id=query_datasource_id,
            config=body,
            user_id=current_user.id,
            db=db,
            force=force,
        )
    except QueryEngineError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": e.code, "message": e.message},
        )
    except Exception as e:
        # 捕获所有其他异常（如 DuckDB ATTACH 失败、网络错误等），返回友好错误
        logger.error("query_canvas 未预期错误 canvas_id=%s error=%s traceback=%s",
                     str(canvas_id), str(e), traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "QUERY_ERROR", "message": f"查询执行失败: {str(e)}"},
        )

    return SuccessResponse(data=result.model_dump(mode="json", by_alias=True))


@router.post("/{canvas_id}/chart-configs", status_code=status.HTTP_201_CREATED)
async def create_canvas_chart_config(
    canvas_id: UUID,
    body: CanvasChartConfigBody,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        chart_type_enum = ChartType(body.chart_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_REQUEST",
                "message": f"不支持的 chart_type: {body.chart_type}",
            },
        )

    canvas = await service.get_by_id(canvas_id, current_user.id)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )

    enriched_query_config = dict(body.query_config)
    if canvas.datasource_id is not None and "datasource_id" not in enriched_query_config:
        enriched_query_config["datasource_id"] = str(canvas.datasource_id)

    cc = await service.create_chart_config(
        chart_type=chart_type_enum,
        query_config=enriched_query_config,
        datasource_id=canvas.datasource_id,
    )
    return SuccessResponse(
        data={
            "chartConfigId": str(cc.id),
            "blockId": body.block_id,
        }
    )


@router.delete("/{canvas_id}")
async def delete_canvas(
    canvas_id: UUID,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    deleted = await service.delete(canvas_id, current_user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )
    return SuccessResponse(data={"message": "已删除"})


@router.post("/{canvas_id}/save-as-report", status_code=status.HTTP_201_CREATED)
async def save_canvas_as_report(
    canvas_id: UUID,
    body: SaveAsReportBody,
    db: AsyncSession = Depends(get_db),
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    try:
        status_enum = ReportStatus(body.status)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_REQUEST",
                "message": f"不支持的 status: {body.status}",
            },
        )

    canvas = await service.get_by_id(canvas_id, current_user.id)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )

    snapshot_blocks = list(canvas.blocks) if canvas.blocks else []
    report = Report(
        user_id=current_user.id,
        title=body.title,
        source_type=ReportSourceType.canvas,
        source_id=canvas.id,
        snapshot_blocks={"blocks": snapshot_blocks, "description": body.description, "datasourceId": str(canvas.datasource_id) if canvas.datasource_id else None},
        status=status_enum,
    )
    db.add(report)
    await db.flush()
    await db.refresh(report)
    return SuccessResponse(data={"report_id": str(report.id)})


@router.post("/{canvas_id}/pin-to-dashboard", status_code=status.HTTP_201_CREATED)
async def pin_canvas_to_dashboard(
    canvas_id: UUID,
    body: PinToDashboardBody,
    db: AsyncSession = Depends(get_db),
    canvas_service: CanvasService = Depends(get_canvas_service),
    dashboard_service: DashboardService = Depends(get_dashboard_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    import traceback as _tb
    try:
        canvas = await canvas_service.get_by_id(canvas_id, current_user.id)
        if canvas is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "画布不存在"},
            )

        # Resolve chart_config_id: use provided or create from chart_config dict
        chart_config_id = body.chart_config_id
        if chart_config_id is None:
            if body.chart_config is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_REQUEST",
                        "message": "必须提供 chartConfigId 或 chartConfig",
                    },
                )
            chart_type_str = body.chart_config.get("chart_type", "bar")
            try:
                chart_type_enum = ChartType(chart_type_str)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_REQUEST",
                        "message": f"不支持的 chart_type: {chart_type_str}",
                    },
                )
            cc = await canvas_service.create_chart_config(
                chart_type=chart_type_enum,
                query_config=body.chart_config.get("query_config", body.chart_config),
                datasource_id=canvas.datasource_id,
                render_config={
                    "renderer": body.chart_config.get("renderer", "echarts"),
                    "palette": body.chart_config.get("palette", "default"),
                },
            )
            await db.flush()
            await db.refresh(cc)
            chart_config_id = cc.id

        dc = await dashboard_service.add_chart(
            dashboard_id=body.dashboard_id,
            user_id=current_user.id,
            chart_config_id=chart_config_id,
            title=canvas.title,
            position=body.position,
        )
        if dc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "NOT_FOUND", "message": "仪表盘或图表配置不存在"},
            )
        return SuccessResponse(data={"chart_id": str(dc.id)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error("pin_to_dashboard 内部错误:\n%s", _tb.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": f"保存到仪表盘失败: {str(e)}"},
        )


@router.get("/{canvas_id}/export/pdf")
async def export_canvas_pdf(
    canvas_id: UUID,
    db: AsyncSession = Depends(get_db),
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
):
    canvas = await service.get_by_id(canvas_id, current_user.id)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )

    raw_blocks = list(canvas.blocks) if canvas.blocks else []
    processed_blocks = []
    logger.info("export_pdf start canvas=%s title=%s blocks=%s",
                canvas.id, canvas.title, len(raw_blocks))

    for block in raw_blocks:
        block_copy = dict(block) if isinstance(block, dict) else block
        btype = str(block_copy.get("type", block_copy.get("blockType", ""))).strip().lower()

        if btype == "chart":
            title = block_copy.get("title", "图表")
            # chart_type 推断：优先取 block.chartType → _chartResult.chartType（最可靠）→ _chartConfig.chartType → renderer（需排除 echarts/recharts）→ 兜底 bar
            chart_result = block_copy.get("_chartResult")
            chart_config = block_copy.get("_chartConfig")
            chart_type = (
                block_copy.get("chartType")
                or (chart_result if isinstance(chart_result, dict) else {}).get("chartType")
                or (chart_config if isinstance(chart_config, dict) else {}).get("chartType")
                or "bar"
            )
            renderer_field = block_copy.get("renderer")
            # 如果 block.renderer 存的是引擎名而非图表类型，用自己的 chart_type 覆盖
            if renderer_field in ("echarts", "recharts") and isinstance(chart_result, dict) and chart_result.get("chartType"):
                chart_type = chart_result["chartType"]
            elif renderer_field and chart_type == "bar" and renderer_field not in ("echarts", "recharts"):
                chart_type = renderer_field
            chart_images = []

            # 1. 优先取前端保存的真实查询结果
            if isinstance(chart_result, dict) and chart_result.get("rows"):
                rows = chart_result["rows"]
                columns = chart_result.get("columns", [])
                if len(columns) >= 2 and rows:
                    x_col = columns[0]
                    value_cols = columns[1:]  # 支持多度量
                    x_labels = [str(r.get(x_col, "")) for r in rows]
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

            # 2. 兜底：旧数据格式
            if not chart_images:
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

            # 3. 重新查询（使用图表配置）
            if not chart_images:
                if isinstance(chart_config, dict) and chart_config.get("datasourceId"):
                    try:
                        from app.services.query_engine import execute_chart_query
                        qc = ChartQueryConfig(
                            dimensions=chart_config.get("dimensions", []),
                            measures=chart_config.get("measures", []),
                            filters=chart_config.get("filters", []),
                            chart_type=chart_config.get("chartType", chart_type),
                            limit=chart_config.get("limit", 20),
                            datasource_id=chart_config.get("datasourceId"),
                        )
                        result = await execute_chart_query(
                            datasource_id=chart_config["datasourceId"],
                            config=qc,
                            user_id=current_user.id,
                            db=db,
                        )
                        if result.rows:
                            x_col = result.columns[0] if result.columns else ""
                            value_cols = result.columns[1:] if len(result.columns) > 1 else []
                            x_labels = [str(r.get(x_col, "")) for r in result.rows]
                            for vcol in value_cols:
                                vals = []
                                for r in result.rows:
                                    v = r.get(vcol, 0)
                                    try:
                                        vals.append(float(v) if v is not None else 0.0)
                                    except (ValueError, TypeError):
                                        vals.append(0.0)
                                if x_labels and vals:
                                    try:
                                        img = render_chart(chart_type, f"{title} - {vcol}", x_labels, vals)
                                        chart_images.append(img)
                                    except Exception:
                                        pass
                    except Exception:
                        pass

            if chart_images:
                block_copy["_chart_images"] = chart_images

        elif btype == "image":
            src = block_copy.get("src", "")
            if src and not src.startswith("data:"):
                data_uri = _image_to_base64_data_uri(src)
                if data_uri:
                    block_copy["_image_data"] = data_uri

        processed_blocks.append(block_copy)

    html_content = _CANVAS_PDF_HTML_TEMPLATE.render(
        canvas={"id": str(canvas.id), "title": canvas.title},
        blocks=processed_blocks,
        exported_at=_now_iso(),
    )

    # 使用子进程调用 Playwright（规避 Windows ProactorEventLoop 不支持子进程问题）
    pdf_bytes = None
    try:
        logger.info("export_pdf invoking playwright (subprocess), html_len=%d", len(html_content))
        pdf_bytes = await _html_to_pdf_subprocess(html_content)
        logger.info("export_pdf playwright OK pdf_len=%d", len(pdf_bytes) if pdf_bytes else 0)
    except Exception as e:
        import traceback as _tb
        logger.warning("Playwright PDF generation failed: %s\n%s", e, _tb.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "PDF_GENERATION_FAILED", "message": f"PDF 生成失败: {type(e).__name__}: {e or repr(e)}"},
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="canvas-{canvas.id}.pdf"; '
                f"filename*=UTF-8\'\'canvas-{canvas.id}.pdf"
            )
        },
    )


@router.post("/ai-recommend")
async def ai_recommend_by_datasource(
    body: AIRecommendCanvasRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    """不需要画布ID，直接根据 datasource_id 推荐图表（用于尚未创建画布的场景）"""
    if body.datasource_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "NO_DATASOURCE", "message": "请提供 datasource_id"},
        )

    ds_result = await db.execute(
        select(DataSource).where(
            DataSource.id == body.datasource_id,
            DataSource.user_id == current_user.id,
        )
    )
    ds = ds_result.scalar_one_or_none()
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    schema_meta = ds.schema_meta or {}
    fields = schema_meta.get("fields", [])
    field_meta = [
        {"name": f.get("name", ""), "category": f.get("category", "")}
        for f in fields
    ]

    try:
        ai_service = AIService(LLMClient(settings))
        suggestions = await ai_service.recommend_charts(field_meta, body.current_config)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CONFIG", "message": str(e)},
        )
    except AINotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_NOT_CONFIGURED", "message": str(e)},
        )

    return SuccessResponse(data={"suggestions": suggestions})


@router.post("/{canvas_id}/ai-recommend")
async def canvas_ai_recommend(
    canvas_id: UUID,
    body: AIRecommendCanvasRequest,
    db: AsyncSession = Depends(get_db),
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    # 解析数据源：优先用画布关联的，否则用 body 传入的（支持尚未创建画布的新场景）
    canvas = await service.get_by_id(canvas_id, current_user.id)
    target_datasource_id: UUID | None = None
    if canvas is not None and canvas.datasource_id is not None:
        target_datasource_id = canvas.datasource_id
    elif body.datasource_id is not None:
        target_datasource_id = body.datasource_id
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "NO_DATASOURCE", "message": "画布未关联数据源，且未提供 datasource_id"},
        )

    ds_result = await db.execute(
        select(DataSource).where(
            DataSource.id == target_datasource_id,
            DataSource.user_id == current_user.id,
        )
    )
    ds = ds_result.scalar_one_or_none()
    if ds is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "数据源不存在"},
        )

    schema_meta = ds.schema_meta or {}
    fields = schema_meta.get("fields", [])
    field_meta = [
        {"name": f.get("name", ""), "category": f.get("category", "")}
        for f in fields
    ]

    try:
        ai_service = AIService(LLMClient(settings))
        suggestions = await ai_service.recommend_charts(field_meta, body.current_config)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CONFIG", "message": str(e)},
        )
    except AINotConfiguredError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AI_NOT_CONFIGURED", "message": str(e)},
        )

    return SuccessResponse(data={"suggestions": suggestions})


@router.post("/{canvas_id}/ai-image")
async def canvas_ai_image(
    canvas_id: UUID,
    body: AIImageBody,
    service: CanvasService = Depends(get_canvas_service),
    current_user: User = Depends(get_current_user),
) -> SuccessResponse:
    canvas = await service.get_by_id(canvas_id, current_user.id)
    if canvas is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "画布不存在"},
        )

    return SuccessResponse(data={"image_url": None, "message": "AI 配图生成将在后续版本中实现"})


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_ = _uuid  # 预留 uuid 别名，防止未使用告警