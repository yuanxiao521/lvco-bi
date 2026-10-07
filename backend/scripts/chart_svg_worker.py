"""图表 SVG 渲染工作进程：Playwright + ECharts(renderer='svg') 把 option 渲染成矢量 SVG。

用法：
    python chart_svg_worker.py <input.json> <output.json>

input.json  : {"charts": [{"key": "0", "option": {...}, "width": 760, "height": 400}, ...]}
output.json : {"svgs": {"0": "data:image/svg+xml;base64,..."}, "errors": {"1": "错误信息"}}

放子进程执行的原因与 pdf_worker.py 一致：规避 Windows 下 Playwright 同步 API
不能在事件循环线程中使用的问题。多张图共用一个浏览器 + 一个页面，避免 N 图开 N 浏览器。
"""
import base64
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

DEFAULT_WIDTH = 760
DEFAULT_HEIGHT = 400

# frontend/node_modules/echarts/dist/echarts.min.js（backend/scripts → 上两级为仓库根）
_ECHARTS_JS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend" / "node_modules" / "echarts" / "dist" / "echarts.min.js"
)


def _echarts_data_uri() -> str:
    """把 echarts.min.js 内联成 data URI，避免 file:// 加载被跨域拦截。"""
    with open(_ECHARTS_JS, "rb") as f:
        return "data:application/javascript;base64," + base64.b64encode(f.read()).decode("ascii")


# 注意：script 内含大量 JS 对象 {}，不能用 str.format（会被当占位符），只能 replace
HTML_SHELL = (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<script src=\"{echarts_uri}\"></script>"
    "</head><body><div id=\"stage\"></div><script>"
    # Y 轴数字格式化，镜像前端 formatYAxisNum（echartsUtils.ts）
    "window.__fmtY = function (v) {"
    "if (v >= 1000000) return (v / 1000000).toFixed(1) + 'M';"
    "if (v >= 10000) return (v / 10000).toFixed(1) + 'w';"
    "if (v >= 1000) return (v / 1000).toFixed(1) + 'k';"
    "return String(v);"
    "};"
    # option 里的 "__lvco_fmt_y__" 是 JS 函数占位符（前端 ChartCard 也做同样替换），
    # 不还原的话 Y 轴每个刻度都会原样打印这串字符串
    "window.__revive = function (o) {"
    "if (o === '__lvco_fmt_y__') return window.__fmtY;"
    "if (Array.isArray(o)) return o.map(window.__revive);"
    "if (o && typeof o === 'object') { for (var k in o) { o[k] = window.__revive(o[k]); } }"
    "return o;"
    "};"
    "window.renderChart = function (cfg) {"
    "return new Promise(function (resolve) {"
    "try {"
    "var box = document.createElement('div');"
    "box.style.cssText = 'width:' + cfg.width + 'px;height:' + cfg.height + 'px;';"
    "document.getElementById('stage').appendChild(box);"
    "var opt = window.__revive(JSON.parse(cfg.optionJson));"
    # 静态导出必须关动画：否则柱子/折线/扇区还在从 0 增长，抓到的是「有轴无数据」的首帧
    "opt.animation = false;"
    "var chart = echarts.init(box, null, { renderer: 'svg' });"
    "chart.setOption(opt);"
    # 等两帧确保 zrender 完成一次绘制再取 DOM
    "requestAnimationFrame(function () { requestAnimationFrame(function () {"
    "var svg = box.firstChild ? box.firstChild.outerHTML : '';"
    "chart.dispose();"
    "box.parentNode.removeChild(box);"
    "resolve(svg);"
    "}); });"
    "} catch (e) { resolve('ERROR:' + (e && e.message ? e.message : String(e))); }"
    "});"
    "};"
    "</script></body></html>"
)


def _svg_to_data_uri(svg: str) -> str:
    """剥出纯 <svg>...</svg> 再 base64 成 data URI（PDF 打印背景可正常显示）。"""
    start = svg.index("<svg")
    end = svg.rindex("</svg>") + len("</svg>")
    pure = svg[start:end]
    return "data:image/svg+xml;base64," + base64.b64encode(pure.encode("utf-8")).decode("ascii")


def render_all(charts: list[dict]) -> dict:
    """逐张渲染，单张失败不影响其他图（记进 errors，由调用方回退 matplotlib）。"""
    svgs: dict[str, str] = {}
    errors: dict[str, str] = {}
    if not charts:
        return {"svgs": svgs, "errors": errors}

    html = HTML_SHELL.replace("{echarts_uri}", _echarts_data_uri())
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        for c in charts:
            key = str(c.get("key"))
            try:
                svg = page.evaluate(
                    "(cfg) => window.renderChart(cfg)",
                    {
                        "optionJson": json.dumps(c.get("option") or {}, ensure_ascii=False),
                        "width": int(c.get("width") or DEFAULT_WIDTH),
                        "height": int(c.get("height") or DEFAULT_HEIGHT),
                    },
                )
            except Exception as e:  # 页面级异常（超时/崩溃）
                errors[key] = f"{type(e).__name__}: {e}"
                continue
            if not svg:
                errors[key] = "SVG 为空"
            elif svg.startswith("ERROR:"):
                errors[key] = svg[len("ERROR:"):]
            else:
                svgs[key] = _svg_to_data_uri(svg)
        browser.close()
    return {"svgs": svgs, "errors": errors}


if __name__ == "__main__":
    input_path, output_path = sys.argv[1], sys.argv[2]
    payload = json.loads(pathlib.Path(input_path).read_text(encoding="utf-8"))
    result = render_all(payload.get("charts") or [])
    pathlib.Path(output_path).write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    print(f"chart_svg_worker done: ok={len(result['svgs'])} failed={len(result['errors'])}")