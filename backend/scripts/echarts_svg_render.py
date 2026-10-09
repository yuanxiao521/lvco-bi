"""Step 1 Demo：验证「ECharts option → 服务端渲染 SVG」可行性（方案 A 试金石）。

依赖：playwright + chromium 已装、echarts.min.js 存在于 node_modules。
产出：打印 SVG 前 300 字符；验证通过后 SVG 已可嵌入 PDF。
"""
import base64
import json
import sys
import time

from playwright.sync_api import sync_playwright

ECHARTS_JS = r"e:\BI\LvcoBI\lvco-bi\frontend\node_modules\echarts\dist\echarts.min.js"

# 直接把 echarts.min.js 内联成 data URI，避免 file:// 跨域/CORS 问题
def _echarts_data_uri() -> str:
    with open(ECHARTS_JS, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:application/javascript;base64,{b64}"

# 注意：script 内含 JS 对象 {}，不能用 str.format（会当成占位符），用 replace 拼接
HTML_SHELL = (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head>"
    "<meta charset=\"utf-8\">"
    "<script src=\"{echarts_uri}\"></script>"
    "</head><body>"
    "<div id=\"c\" style=\"width:640px;height:400px;\"></div>"
    "<script>"
    "window.renderOption = function(optJson) {"
    "var opt = JSON.parse(optJson);"
    # 关键：关掉入场动画。否则柱子/折线/扇区还在从 0 增长，抓到的是「有轴无数据」的首帧
    "opt.animation = false;"
    "var chart = echarts.init(document.getElementById('c'), null, {renderer: 'svg'});"
    "chart.setOption(opt);"
    # 等两帧确保 zrender 已完成一次绘制，再取 DOM
    "requestAnimationFrame(function () {"
    "requestAnimationFrame(function () {"
    "var dom = document.getElementById('c');"
    "window.__svg = dom.firstChild ? dom.firstChild.outerHTML : '';"
    "});"
    "});"
    "};"
    "</script>"
    "</body></html>"
)


def option_to_svg(option: dict, *, timeout: float = 5.0) -> str:
    """把 ECharts option 渲染成 SVG 字符串。失败抛异常（调用方决定回退）。"""
    # 用 replace 而非 format：HTML/JS 内含大量 {}（JS 对象字面量），format 会误抓
    html = HTML_SHELL.replace("{echarts_uri}", _echarts_data_uri())
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.set_content(html, wait_until="load")
        page.evaluate("window.renderOption(%s)" % json.dumps(json.dumps(option, ensure_ascii=False)))
        page.wait_for_function("window.__svg !== undefined", timeout=int(timeout * 1000))
        svg = page.evaluate("window.__svg")
        browser.close()
        if not svg:
            raise RuntimeError("SVG 为空：" + " | ".join(errors[:3]))
        return svg


if __name__ == "__main__":
    demo_options = {
        "bar": {
            "title": {"text": "各渠道销售额"},
            "tooltip": {"trigger": "axis"},
            "xAxis": {"type": "category", "data": ["抖音", "快手", "淘宝"]},
            "yAxis": {"type": "value"},
            "series": [{"name": "销售额", "type": "bar", "data": [100, 200, 150]}],
        },
        "line": {
            "title": {"text": "月度趋势"},
            "xAxis": {"type": "category", "data": ["1月", "2月", "3月", "4月"]},
            "yAxis": {"type": "value"},
            "series": [{"name": "订单量", "type": "line", "data": [880, 1250, 1090, 1560]}],
        },
        "pie": {
            "title": {"text": "品类占比"},
            "series": [{
                "type": "pie", "radius": "60%",
                "data": [{"name": "数码", "value": 4520},
                         {"name": "服饰", "value": 3180},
                         {"name": "家居", "value": 2100}],
            }],
        },
    }

    t0 = time.time()
    for name, opt in demo_options.items():
        print(f"\n===== chart_type={name} =====")
        svg = option_to_svg(opt)
        has_rect = "<rect" in svg or "<path" in svg
        print(f"SVG length: {len(svg)}")
        print(f"含图形节点(rect/path): {has_rect}")
        print(f"含中文标题: {'各渠道销售额' in svg or '月度趋势' in svg or '品类占比' in svg}")
    print(f"\n总耗时: {time.time() - t0:.2f}s")

    # T1 冒烟：bar 的 SVG 头部
    svg = option_to_svg(demo_options["bar"])
    print("\n--- bar SVG 头部（前 300 字符）---")
    print(svg[:300])
    ok = "<svg" in svg and "xmlns" in svg
    print("\nStep 1 验证完成：OK（SVG 渲染可行）" if ok else "\nStep 1 验证失败")
    # 存一份 SVG 文件供人工查看
    import pathlib
    out = pathlib.Path(__file__).parent / "_demo_bar.svg"
    # 如果外层是 div 包装，剥出纯 svg 便于直接查看
    if "<svg" in svg:
        start = svg.index("<svg")
        end = svg.rindex("</svg>") + len("</svg>")
        out.write_text(svg[start:end], encoding="utf-8")
        print(f"\n已保存示例 SVG: {out}")