from datetime import datetime
from typing import Any


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<title>{title}</title>
<style>
  body {{ font-family: "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
         color: #1A2332; background: #FFFFFF; margin: 40px; line-height: 1.6; }}
  h1 {{ color: #2BB5A0; border-bottom: 2px solid #2BB5A0; padding-bottom: 8px; }}
  h2 {{ color: #1A2332; margin-top: 32px; }}
  .meta {{ color: #8B97A8; font-size: 12px; margin-bottom: 24px; }}
  .block {{ margin-bottom: 20px; padding: 16px; border: 1px solid #E2E8F0; border-radius: 10px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
  th, td {{ border: 1px solid #E2E8F0; padding: 6px 10px; text-align: left; font-size: 13px; }}
  th {{ background: #E8F7F4; color: #2BB5A0; }}
  pre {{ background: #F8FAFB; padding: 12px; border-radius: 6px; font-size: 13px;
         white-space: pre-wrap; word-break: break-word; }}
  .footer {{ margin-top: 48px; color: #8B97A8; font-size: 11px; text-align: center; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">生成时间: {generated_at} | 状态: {status}</div>
{body}
<div class="footer">由 Lvco BI 自动生成</div>
</body>
</html>
"""


def _escape(value: Any) -> str:
    s = str(value) if value is not None else ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_blocks_to_html(blocks: list[dict[str, Any]] | None) -> str:
    if not blocks:
        return "<p>报表内容为空。</p>"

    parts: list[str] = []
    for block in blocks:
        btype = block.get("type", "text")
        content = block.get("content", "")

        if btype in ("title", "h1"):
            parts.append(f'<h1>{_escape(content)}</h1>')
        elif btype in ("h2",):
            parts.append(f'<h2>{_escape(content)}</h2>')
        elif btype == "text":
            parts.append(f'<div class="block"><pre>{_escape(content)}</pre></div>')
        elif btype == "table":
            data = block.get("data", [])
            if isinstance(data, list) and data:
                columns = list(data[0].keys()) if data else []
                rows_html = "".join(
                    "<tr>" + "".join(f"<td>{_escape(r.get(c))}</td>" for c in columns) + "</tr>"
                    for r in data
                )
                header_html = "".join(f"<th>{_escape(c)}</th>" for c in columns)
                parts.append(
                    f'<div class="block"><h3>{_escape(content or "数据表")}</h3>'
                    f'<table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table></div>'
                )
            else:
                parts.append(f'<div class="block">{_escape(content)}</div>')
        elif btype == "chart":
            if block.get("_chart_image"):
                parts.append(f'<div class="block block-chart"><img src="{_escape(block["_chart_image"])}" style="max-width:100%;" /></div>')
            else:
                data = block.get("data", {})
                columns = data.get("columns", []) if isinstance(data, dict) else []
                rows = data.get("rows", []) if isinstance(data, dict) else []
                if rows and columns:
                    header_html = "".join(f"<th>{_escape(c)}</th>" for c in columns)
                    rows_html = "".join(
                        "<tr>" + "".join(f"<td>{_escape(r.get(c))}</td>" for c in columns) + "</tr>"
                        for r in rows
                    )
                    parts.append(
                        f'<div class="block"><h3>{_escape(content or block.get("title", "图表"))}</h3>'
                        f'<table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table></div>'
                    )
                else:
                    parts.append(f'<div class="block"><h3>{_escape(content)}</h3><p>暂无数据</p></div>')
        elif btype == "image":
            url = block.get("src", "")
            parts.append(f'<div class="block"><img src="{_escape(url)}" style="max-width:100%;" /></div>')
        elif btype == "divider":
            parts.append('<hr style="border:none;border-top:1px solid #E2E8F0;margin:24px 0;" />')
        else:
            parts.append(f'<div class="block">{_escape(content)}</div>')

    return "\n".join(parts)


def render_report_html(title: str, status: str, snapshot_blocks: list[dict[str, Any]] | None) -> str:
    return HTML_TEMPLATE.format(
        title=_escape(title),
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        status=_escape(status),
        body=render_blocks_to_html(snapshot_blocks),
    )