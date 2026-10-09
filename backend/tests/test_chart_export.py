"""图表导出（ECharts SVG）单测：
- option 复用 render_chart 构建器（与 AI 推荐/前端同一份配置）
- 服务端渲染出的 SVG 必须带真实数据（防止抓到动画首帧的「有轴无数据」回归）
- JS 函数占位符 __lvco_fmt_y__ 必须被还原，不能原样打进 Y 轴
- 失败/空输入的兜底契约：返回空串，由调用方回退 matplotlib
"""
import asyncio
import base64
import re

import pytest

from app.services.chart_export import build_option, render_options_to_svg_uris, rows_to_matrix


def test_rows_to_matrix_supports_dict_and_list_rows():
    columns = ["渠道", "销售额"]
    assert rows_to_matrix(columns, [{"渠道": "抖音", "销售额": 120}]) == [["抖音", 120]]
    assert rows_to_matrix(columns, [["抖音", 120]]) == [["抖音", 120]]


def test_build_option_from_block_rows():
    """画布/报表里的 _chartResult（columns + dict rows）能直接构建出 option。"""
    option = asyncio.run(build_option(
        "bar",
        "各渠道销售额",
        ["渠道", "销售额"],
        [{"渠道": "抖音", "销售额": 120}, {"渠道": "快手", "销售额": 200}],
    ))
    assert isinstance(option, dict) and option.get("series")
    assert option["series"][0].get("data")


def test_build_option_returns_none_for_empty_or_unknown():
    assert asyncio.run(build_option("bar", "空数据", ["渠道"], [])) is None
    assert asyncio.run(build_option("bar", "空数据", [], [])) is None
    assert asyncio.run(build_option("no_such_type", "未知图型", ["渠道", "值"],
                                    [{"渠道": "抖音", "值": 1}])) is None


def test_render_empty_options_returns_empty_list():
    assert asyncio.run(render_options_to_svg_uris([])) == []
    assert asyncio.run(render_options_to_svg_uris([None, {}])) == ["", ""]


def test_render_option_to_svg_uri_with_real_data():
    pytest.importorskip("playwright")
    option = {
        "title": {"text": "各渠道销售额"},
        "xAxis": {"type": "category", "data": ["抖音", "快手"]},
        "yAxis": {"type": "value", "axisLabel": {"formatter": "__lvco_fmt_y__"}},
        "series": [{"name": "销售额", "type": "bar", "data": [120, 200]}],
    }
    uris = asyncio.run(render_options_to_svg_uris([option]))
    assert uris[0].startswith("data:image/svg+xml;base64,")
    svg = base64.b64decode(uris[0].split(",", 1)[1]).decode("utf-8")

    assert "<svg" in svg and "<rect" in svg
    # 关键回归：关动画后柱体必须真有高度（抓到首帧时所有 rect 高度≈0）
    heights = [float(h) for h in re.findall(r'height="([\d.]+)"', svg)]
    assert heights and max(heights) > 20
    # 中文与占位符还原
    assert "抖音" in svg
    assert "__lvco_fmt_y__" not in svg


def test_canvas_export_html_to_pdf_with_svg_chart():
    """端到端：图表块 SVG → 画布模板 → Playwright → PDF（验证 SVG 能进 PDF 且不报错）。"""
    pytest.importorskip("playwright")
    from app.api.v1.canvases import _CANVAS_PDF_HTML_TEMPLATE, _html_to_pdf_subprocess

    option = asyncio.run(build_option(
        "bar", "各渠道销售额", ["渠道", "销售额"],
        [{"渠道": "抖音", "销售额": 120}, {"渠道": "快手", "销售额": 200}],
    ))
    uri = asyncio.run(render_options_to_svg_uris([option]))[0]
    assert uri.startswith("data:image/svg+xml;base64,")

    html = _CANVAS_PDF_HTML_TEMPLATE.render(
        canvas={"id": "c-test", "title": "测试画布"},
        blocks=[{"type": "chart", "title": "各渠道销售额", "_chart_images": [uri]}],
        exported_at="2026-09-24",
    )
    pdf = asyncio.run(_html_to_pdf_subprocess(html))
    assert pdf[:4] == b"%PDF" and len(pdf) > 3000