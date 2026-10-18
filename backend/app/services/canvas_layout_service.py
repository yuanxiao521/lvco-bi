# -*- coding: utf-8 -*-
"""报告式自动布局（后端移植版）。

与前端 `frontend/src/utils/reportLayout.ts` **逐函数直译对应**，两边必须产出
相同的坐标——后端用它落库，前端用它渲染，偏差会让块错位/编号错乱。

直译对应关系：
  resolveGeometry      -> resolve_geometry
  estimateTextHeight   -> estimate_text_height
  assignBlockLabels    -> assign_block_labels
  estimateBlockHeight  -> estimate_block_height
  deriveCursor         -> derive_cursor
  nextChartSlot        -> next_chart_slot
  nextFullWidthSlot    -> next_full_width_slot
  applyReportLayout    -> apply_report_layout

注意事项：
- JS 的 Math.round 对正数是 floor(x + 0.5)，Python 的 round 是银行家舍入，
  故统一走 _round() 保证一致。
- blocks 元素是 dict（JSON），字段与前端 CanvasBlock 一致：
  type / blockId / title / content / x / y / width / height / datasourceId ...
"""
from __future__ import annotations

import math
from functools import cmp_to_key
from typing import Any

# 报告式布局常量（与前端 REPORT 完全一致）
REPORT = {
    "marginX": 20,     # 画布左右留白
    "gapY": 24,        # 块间距
    "colGap": 20,      # 双列列间距
    "startY": 32,      # 首块起始 y
    "chartH": 320,     # 图表块默认高度
    "kpiH": 132,       # KPI 卡片默认高度（只有一个大数字，不需要图表那么高）
    "minColW": 360,    # 双列时每列最小宽度
    "fallbackW": 980,  # 未测量到画布实宽时的兜底通栏宽
}

TEXT_FONT_PX = 14
TEXT_LINE_HEIGHT = 22
TEXT_H_PADDING = 20
TEXT_V_PADDING = 40


def _round(x: float) -> int:
    """JS Math.round 语义（正数 floor(x + 0.5)），与 Python 银行家舍入区分。"""
    return int(math.floor(x + 0.5))


def resolve_geometry(width: float | None = None) -> dict:
    """由画布内容区实宽推导几何；width 缺省/非法时直接用 fallbackW（已扣边距的通栏宽）。"""
    if width is not None and width > 0:
        avail = _round(max(320, width - REPORT["marginX"] * 2))
    else:
        avail = REPORT["fallbackW"]
    two = avail >= REPORT["minColW"] * 2 + REPORT["colGap"]
    chart_w = _round((avail - REPORT["colGap"]) / 2) if two else avail
    # KPI 卡片一行 3 个：单张更窄更矮，避免"一个数字占半屏"的丑陋默认（实测反馈）
    kpi_w = _round((avail - REPORT["colGap"] * 2) / 3)
    return {
        "fullW": avail,
        "chartW": chart_w,
        "kpiW": kpi_w,
        "cols": 2 if two else 1,
        "leftX": REPORT["marginX"],
        "rightX": REPORT["marginX"] + chart_w + REPORT["colGap"],
        "midX": REPORT["marginX"] + chart_w + REPORT["colGap"] / 2,
    }


def estimate_text_height(content: str, width_px: float | None, font_size: int = TEXT_FONT_PX) -> int:
    """估算文本块渲染高度（中文字符宽 ≈ 字号）。"""
    text = content or ""
    usable = max(80, (width_px or REPORT["fallbackW"]) - TEXT_H_PADDING * 2)
    chars_per_line = max(8, math.floor(usable / font_size))
    lines = 0
    for seg in text.split("\n"):
        lines += max(1, math.ceil((len(seg) or 1) / chars_per_line))
    return lines * TEXT_LINE_HEIGHT + TEXT_V_PADDING


def _pos_of(b: dict) -> tuple[float, float] | None:
    x, y = b.get("x"), b.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return (x, y)
    return None


def assign_block_labels(blocks: list[dict]) -> list[str]:
    """稳定可见编号（A1/A2/B1…），与前端 assignBlockLabels 同规则。

    按坐标排序（y 容差 10px 归同一行，行内按 x），行字母 A→Z；
    无坐标的块排在最后继续累加。返回与 blocks 下标对应的编号数组。
    """
    out = [""] * len(blocks)

    def cmp(a: dict, b: dict) -> int:
        pa, pb = a["p"], b["p"]
        if pa is None or pb is None:
            return (0 if pa else 1) - (0 if pb else 1)
        if abs(pa[1] - pb[1]) <= 10:
            return -1 if pa[0] < pb[0] else (1 if pa[0] > pb[0] else 0)
        return -1 if pa[1] < pb[1] else 1

    order = sorted(
        ({"i": i, "p": _pos_of(b)} for i, b in enumerate(blocks)),
        key=cmp_to_key(cmp),
    )

    row_keys: list[float] = []
    row_letters: dict[float, str] = {}
    col_idx: dict[str, int] = {}
    extra_rows = 0
    for item in order:
        p = item["p"]
        if p is not None:
            key = next((k for k in row_keys if abs(k - p[1]) <= 10), None)
            if key is None:
                key = p[1]
                row_keys.append(key)
            if key not in row_letters:
                row_letters[key] = chr(65 + len(row_letters))
            row = row_letters[key]
        else:
            row = chr(65 + len(row_letters))
            row_letters[-1 - extra_rows] = row
            extra_rows += 1
        col = col_idx.get(row, 0) + 1
        col_idx[row] = col
        out[item["i"]] = f"{row}{col}"
    return out


def estimate_block_height(block: dict, geo: dict | None = None) -> int:
    """块高估算：显式 height 优先，否则按内容/类型估算。"""
    geo = geo or resolve_geometry()
    explicit = block.get("height")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return int(explicit)

    t = block.get("type")
    if t == "h1":
        c = block.get("content") or ""
        return math.ceil((len(c) or 1) / 40) * 34 + 24 if c else 56
    if t == "h2":
        c = block.get("content") or ""
        return math.ceil((len(c) or 1) / 50) * 30 + 20 if c else 44
    if t == "text":
        c = block.get("content") or ""
        w = block.get("width") if isinstance(block.get("width"), (int, float)) else geo["fullW"]
        return estimate_text_height(c, w)
    if t == "chart":
        if block.get("chartType") == "kpi_card":
            return REPORT["kpiH"]
        return REPORT["chartH"]
    return 300


def block_bottom(block: dict, index: int = 0, geo: dict | None = None) -> int:
    """块占用底部 y（无坐标块按自动铺位估算）。"""
    geo = geo or resolve_geometry()
    p = _pos_of(block)
    y = int(math.floor(index / 2) * 400) if p is None else p[1]
    return y + estimate_block_height(block, geo)


def _is_placed(b: dict) -> bool:
    return _pos_of(b) is not None


def _bottom_of(b: dict, geo: dict) -> int:
    return int(b["y"]) + estimate_block_height(b, geo)


def _without_height(b: dict) -> dict:
    rec = dict(b)
    rec.pop("height", None)
    return rec


def _width_of(b: dict, geo: dict) -> float:
    w = b.get("width")
    if isinstance(w, (int, float)):
        return w
    t = str(b.get("type") or "")
    return geo["fullW"] if t in ("text", "h1", "h2") else geo["chartW"]


def derive_cursor(blocks: list[dict], width: float | None = None, start_y: int | None = None) -> dict:
    """从现有 blocks 推导双列游标（无坐标块跳过）。"""
    geo = resolve_geometry(width)
    sy = start_y if start_y is not None else REPORT["startY"]
    left_bottom, right_bottom = sy, sy
    for b in blocks:
        if not _is_placed(b):
            continue
        w = _width_of(b, geo)
        bottom = _bottom_of(b, geo)
        if w >= geo["fullW"] - 40:
            left_bottom = max(left_bottom, bottom)
            right_bottom = max(right_bottom, bottom)
        elif b["x"] + w / 2 < geo["midX"]:
            left_bottom = max(left_bottom, bottom)
        else:
            right_bottom = max(right_bottom, bottom)
    return {"leftBottom": left_bottom, "rightBottom": right_bottom}


def next_chart_slot(blocks: list[dict], width: float | None = None) -> dict:
    """下一个图表块位置：放进底部较低的列（左列平局优先）。"""
    geo = resolve_geometry(width)
    sy = REPORT["startY"]
    cur = derive_cursor(blocks, width)
    if cur["leftBottom"] <= cur["rightBottom"]:
        lb = cur["leftBottom"]
        return {"x": geo["leftX"], "y": lb + REPORT["gapY"] if lb > sy else lb}
    rb = cur["rightBottom"]
    return {"x": geo["rightX"], "y": rb + REPORT["gapY"] if rb > sy else rb}


def is_kpi_block(b: dict) -> bool:
    """是否 KPI 卡片块（chart 的 kpi_card 变体）。"""
    return b.get("type") == "chart" and b.get("chartType") == "kpi_card"


def next_kpi_slot(blocks: list[dict], width: float | None = None) -> dict:
    """KPI 卡片槽位：同一行最多 3 个，接在该行最右块之后；行满另起一行。"""
    geo = resolve_geometry(width)
    sy = REPORT["startY"]
    kpi_w = geo["kpiW"]
    placed = [b for b in blocks if _is_placed(b)]
    kpis = [b for b in placed if is_kpi_block(b)]
    if kpis:
        last_y = max(b["y"] for b in kpis)
        row = [b for b in kpis if abs(b["y"] - last_y) <= 5]
        if len(row) < 3:
            max_x = max(b["x"] for b in row)
            return {"x": max_x + kpi_w + REPORT["colGap"], "y": last_y}
        return {"x": geo["leftX"], "y": last_y + REPORT["kpiH"] + REPORT["gapY"]}
    # 尚无 KPI：放在现有块底部之后（空画布从 startY 起）
    bottom = max((_bottom_of(b, geo) for b in placed), default=sy)
    return {"x": geo["leftX"], "y": bottom + REPORT["gapY"] if bottom > sy else bottom}


def next_full_width_slot(blocks: list[dict], width: float | None = None) -> dict:
    """下一个通栏块位置：两列最深底部 + 间距。"""
    geo = resolve_geometry(width)
    sy = REPORT["startY"]
    cur = derive_cursor(blocks, width)
    max_y = max(cur["leftBottom"], cur["rightBottom"])
    return {"x": geo["leftX"], "y": max_y + REPORT["gapY"] if max_y > sy else max_y}


def apply_report_layout(blocks: list[dict], width: float | None = None) -> list[dict]:
    """全量重排（arrange_layout）：按现有顺序重写所有块坐标。

    KPI 卡片单独成行、一行最多 3 个（kpiW × kpiH），普通图表仍是双列网格。
    """
    geo = resolve_geometry(width)
    sy = REPORT["startY"]
    left_bottom, right_bottom = sy, sy
    kpi_row_y: int | None = None   # 当前 KPI 行 y（None=不在 KPI 行内）
    kpi_row_n = 0                  # 当前行已放几个
    out: list[dict] = []
    for b in blocks:
        t = b.get("type")
        if is_kpi_block(b):
            kpi_w = geo["kpiW"]
            if kpi_row_y is None or kpi_row_n >= 3:
                base_y = max(left_bottom, right_bottom)
                kpi_row_y = base_y + REPORT["gapY"] if base_y > sy else base_y
                kpi_row_n = 0
            x = geo["leftX"] + kpi_row_n * (kpi_w + REPORT["colGap"])
            out.append({**b, "x": x, "y": kpi_row_y, "width": kpi_w, "height": REPORT["kpiH"]})
            kpi_row_n += 1
            # 行（或未满行）底部推进游标：后续块不会叠到 KPI 上
            bottom = kpi_row_y + REPORT["kpiH"]
            left_bottom = max(left_bottom, bottom)
            right_bottom = max(right_bottom, bottom)
            if kpi_row_n >= 3:
                kpi_row_y = None
                kpi_row_n = 0
            continue
        # 非 KPI 块：结束当前 KPI 行
        kpi_row_y = None
        kpi_row_n = 0
        if t == "chart":
            use_left = geo["cols"] == 1 or left_bottom <= right_bottom
            col_bottom = left_bottom if use_left else right_bottom
            y = col_bottom + REPORT["gapY"] if col_bottom > sy else col_bottom
            if use_left:
                left_bottom = y + REPORT["chartH"]
            else:
                right_bottom = y + REPORT["chartH"]
            out.append({**b, "x": geo["leftX"] if use_left else geo["rightX"],
                        "y": y, "width": geo["chartW"], "height": REPORT["chartH"]})
            continue
        # 通栏块：叙事块重置为自适应高度，图片/其他保留自身高度
        cleaned = b if t == "image" else _without_height(b)
        h = estimate_block_height(cleaned, geo)
        max_y = max(left_bottom, right_bottom)
        y = max_y + REPORT["gapY"] if max_y > sy else max_y
        origin_w = b.get("width") if isinstance(b.get("width"), (int, float)) else None
        w = min(origin_w, geo["fullW"]) if (t == "image" and origin_w) else geo["fullW"]
        bottom = y + h
        left_bottom = bottom
        right_bottom = bottom
        out.append({**cleaned, "x": geo["leftX"], "y": y, "width": w})
    return out
