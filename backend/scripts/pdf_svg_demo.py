"""Step 2 Demo：验证「ECharts SVG + HTML → Playwright → PDF」完整链路（方案 A 端到端）。

- 复用 scripts/echarts_svg_render.option_to_svg 生成 SVG
- 复用 scripts/pdf_worker.py 打印 PDF
- 同时放一张 matplotlib 兜底 PNG 做对比（验证回退分支仍可用）

产出：backend/scripts/_demo_report.pdf  （浏览器打开查看还原度）
"""
import base64
import io
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))

from echarts_svg_render import option_to_svg  # noqa: E402

# ── 1. 用 ECharts option 生成 SVG ─────────────────────────────────────
BAR_OPT = {
    "title": {"text": "各渠道销售额（ECharts SVG）"},
    "tooltip": {"trigger": "axis"},
    "legend": {"data": ["销售额"]},
    "xAxis": {"type": "category", "data": ["抖音", "快手", "淘宝", "拼多多"]},
    "yAxis": {"type": "value"},
    "series": [{"name": "销售额", "type": "bar", "data": [120, 200, 150, 180]}],
}
LINE_OPT = {
    "title": {"text": "月度订单趋势（ECharts SVG）"},
    "tooltip": {"trigger": "axis"},
    "xAxis": {"type": "category", "data": ["1月", "2月", "3月", "4月", "5月"]},
    "yAxis": {"type": "value"},
    "series": [{"name": "订单量", "type": "line", "data": [880, 1250, 1090, 1560, 1420]}],
}
PIE_OPT = {
    "title": {"text": "品类占比（ECharts SVG）"},
    "legend": {"orient": "vertical", "left": "left"},
    "series": [{
        "type": "pie", "radius": "60%",
        "data": [{"name": "数码", "value": 4520},
                 {"name": "服饰", "value": 3180},
                 {"name": "家居", "value": 2100},
                 {"name": "食品", "value": 1560}],
    }],
}


def _svg_to_data_uri(svg: str) -> str:
    """剥出纯 <svg>...</svg>，base64 编码成 data URI（PDF 打印 background 可显示）。"""
    start = svg.index("<svg")
    end = svg.rindex("</svg>") + len("</svg>")
    pure = svg[start:end]
    b64 = base64.b64encode(pure.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _matplotlib_fallback_png() -> str:
    """复现现有兜底：matplotlib 画 PNG（验证回退分支仍可用）。"""
    sys.path.insert(0, str(pathlib.Path(r"e:\BI\LvcoBI\lvco-bi\backend").resolve()))
    from app.services.chart_renderer import render_bar  # noqa: E402

    uri = render_bar("各渠道销售额（matplotlib 兜底）", ["抖音", "快手", "淘宝", "拼多多"], [120, 200, 150, 180])
    return uri


# ── 2. 组装 HTML（与 pdf_export.HTML_TEMPLATE 同风格）──────────────────
def build_html(svg_imgs: list[str], fallback_png: str) -> str:
    def img_html(uri: str) -> str:
        return (f'<img src="{uri}" style="max-width:100%; height:auto; '
                'border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.08); margin-bottom:8px;" />')

    blocks = ""
    for svg in svg_imgs:
        blocks += f'<div class="block block-chart">{img_html(_svg_to_data_uri(svg))}</div>'
    blocks += f'<div class="block block-chart">{img_html(fallback_png)}</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>LvcoBI PDF 高保真 Demo</title>
<style>
  body {{ font-family: "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
         color: #1A2332; background: #FFFFFF; margin: 40px; line-height: 1.6; }}
  h1 {{ color: #2BB5A0; border-bottom: 2px solid #2BB5A0; padding-bottom: 8px; }}
  .meta {{ color: #8B97A8; font-size: 12px; margin-bottom: 24px; }}
  .block {{ margin-bottom: 20px; padding: 16px; border: 1px solid #E2E8F0; border-radius: 10px; }}
  .footer {{ margin-top: 48px; color: #8B97A8; font-size: 11px; text-align: center; }}
</style>
</head>
<body>
<h1>PDF 导出高保真方案 — 链路验证</h1>
<div class="meta">生成时间: 2026-09-24 | 演示: ECharts SVG vs matplotlib 兜底</div>
{blocks}
<div class="footer">由 Lvco BI Demo 生成</div>
</body>
</html>"""


def main() -> None:
    print("① 渲染 ECharts SVG...")
    svg_bar = option_to_svg(BAR_OPT)
    svg_line = option_to_svg(LINE_OPT)
    svg_pie = option_to_svg(PIE_OPT)
    print(f"   bar={len(svg_bar)}B line={len(svg_line)}B pie={len(svg_pie)}B")

    print("② 渲染 matplotlib 兜底 PNG（现有逻辑）...")
    fallback_png = _matplotlib_fallback_png()

    print("③ 组装 HTML...")
    html = build_html([svg_bar, svg_line, svg_pie], fallback_png)
    html_path = HERE / "_demo_report.html"
    html_path.write_text(html, encoding="utf-8")

    print("④ Playwright → PDF...")
    pdf_path = HERE / "_demo_report.pdf"
    # 复用现有 pdf_worker.py 的子进程打印
    result = subprocess.run(
        [sys.executable, str(HERE / "pdf_worker.py"), str(html_path), str(pdf_path)],
        capture_output=True, text=True, timeout=120,
    )
    print(result.stdout)
    if result.returncode != 0:
        print("worker stderr:", result.stderr)
        sys.exit(1)

    print(f"\n✅ 端到端验证完成，请打开: {pdf_path}")


if __name__ == "__main__":
    main()