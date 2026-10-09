"""图表导出：把画布/报表里的图表块用 ECharts 服务端渲染成 SVG。

设计要点：
- option 复用 render_chart 工具已有的构建器（与 AI 推荐链路、前端 buildMultiMeasureOption 同一份配置），
  所以导出图与前端画布上的图型/配色/双 Y 轴行为天然一致，不再维护第二套 matplotlib 图型映射。
- 渲染走 scripts/chart_svg_worker.py 子进程（Playwright 同步 API 不能在事件循环线程里跑），
  一次浏览器批量渲染 N 张图。
- 任何一步失败都返回空串，由调用方回退原有的 matplotlib 兜底，保证导出永远出得来。
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 760
DEFAULT_HEIGHT = 400


def rows_to_matrix(columns: list, rows: list) -> list[list]:
    """把查询结果行（[{列名: 值}]）转成 render_chart 需要的二维数组。"""
    matrix: list[list] = []
    for row in rows or []:
        if isinstance(row, dict):
            matrix.append([row.get(c) for c in columns])
        elif isinstance(row, (list, tuple)):
            matrix.append(list(row))
    return matrix


def _avoid_title_legend_overlap(option: dict) -> None:
    """ECharts 的 title 与 legend 默认都贴顶，同时出现会叠字（导出图必须可读）。
    grid.top 在有多度量图例时已是 40，把图例压到标题下方即可，不改其他配置。"""
    title = option.get("title")
    legend = option.get("legend")
    if not title or not isinstance(legend, dict) or not legend.get("show", True):
        return
    top = legend.get("top")
    if isinstance(top, (int, float)) and top < 24:
        legend["top"] = 26


async def build_option(chart_type: str, title: str, columns: list, rows: list) -> dict | None:
    """构建图表块的 ECharts option；无法构建（数据为空/图型不支持）时返回 None。"""
    if not columns or not rows:
        return None
    from app.services.agent_tools import render_chart_tool

    matrix = rows_to_matrix(columns, rows)
    if not matrix:
        return None
    try:
        raw = await render_chart_tool.execute(
            chart_type=chart_type or "bar",
            title=title or "图表",
            columns=list(columns),
            rows=matrix,
        )
        payload = json.loads(raw)
    except Exception as e:
        logger.warning("build_option failed: chart_type=%s err=%s", chart_type, e)
        return None
    option = payload.get("option") if isinstance(payload, dict) else None
    if not isinstance(option, dict) or not option:
        return None
    _avoid_title_legend_overlap(option)
    return option


def _worker_script() -> str:
    # __file__ = backend/app/services/chart_export.py → 上三级到 backend/
    backend_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(backend_root, "scripts", "chart_svg_worker.py")


async def render_options_to_svg_uris(
    options: list[dict | None],
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> list[str]:
    """批量把 option 渲染成 SVG data URI；失败或 option 为空的位置返回 ""（调用方回退）。"""
    result = [""] * len(options)
    charts = [
        {"key": str(i), "option": opt, "width": width, "height": height}
        for i, opt in enumerate(options)
        if isinstance(opt, dict) and opt
    ]
    if not charts:
        return result

    script_path = _worker_script()
    if not os.path.exists(script_path):
        logger.warning("chart_svg_worker.py not found at %s", script_path)
        return result

    in_path = out_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as f:
            json.dump({"charts": charts}, f, ensure_ascii=False)
            in_path = f.name
        out_path = in_path.replace(".json", ".out.json")

        # 与 pdf_worker 一致：用同步 subprocess.run 包 to_thread，规避 Windows ProactorEventLoop 问题
        proc = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, script_path, in_path, out_path],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            logger.warning("chart_svg_worker failed (rc=%s): %s", proc.returncode, proc.stderr[:500])
            return result

        with open(out_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        svgs = payload.get("svgs") or {}
        errors = payload.get("errors") or {}
        for i in range(len(options)):
            result[i] = svgs.get(str(i), "")
        if errors:
            logger.warning("chart_svg render partial failures: %s", errors)
    except Exception as e:
        logger.warning("render_options_to_svg_uris failed: %s", e)
        return [""] * len(options)
    finally:
        for p in (in_path, out_path):
            if not p:
                continue
            try:
                os.unlink(p)
            except (FileNotFoundError, PermissionError):
                pass
    return result