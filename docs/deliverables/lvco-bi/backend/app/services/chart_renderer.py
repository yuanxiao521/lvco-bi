import base64
import io
import matplotlib
matplotlib.use('Agg')  # non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
from typing import Any


# Try to find a Chinese font on the system
def _get_chinese_font():
    """Find a Chinese-capable font on the system."""
    candidates = ['Microsoft YaHei', 'SimHei', 'SimSun', 'Noto Sans CJK SC', 'WenQuanYi Micro Hei']
    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            return name
    return 'sans-serif'


_CHART_COLORS = ['#2BB5A0', '#4A90D9', '#F5A623', '#D0021B', '#7B68EE', '#50C878', '#FF6B6B', '#45B7D1']
_PDF_DPI = 150


def _setup_style():
    font = _get_chinese_font()
    plt.rcParams['font.family'] = font
    plt.rcParams['axes.unicode_minus'] = False


def render_bar(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render a bar chart to base64 PNG. Returns data:image/png;base64,..."""
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    colors = [_CHART_COLORS[i % len(_CHART_COLORS)] for i in range(len(labels))]
    ax.bar(labels, values, color=colors, edgecolor='white', linewidth=0.5)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')
    ax.tick_params(axis='x', rotation=30, labelsize=9)
    ax.tick_params(axis='y', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_line(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render a line chart to base64 PNG."""
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(labels, values, marker='o', color='#2BB5A0', linewidth=2, markersize=6)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')
    ax.tick_params(axis='x', rotation=30, labelsize=9)
    ax.tick_params(axis='y', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_area(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render an area chart to base64 PNG."""
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.fill_between(range(len(labels)), values, alpha=0.25, color='#2BB5A0')
    ax.plot(labels, values, marker='o', color='#2BB5A0', linewidth=2, markersize=6)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')
    ax.tick_params(axis='x', rotation=30, labelsize=9)
    ax.tick_params(axis='y', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_pie(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render a pie chart to base64 PNG."""
    _setup_style()

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct='%1.1f%%',
        colors=_CHART_COLORS[:len(labels)], startangle=90,
        wedgeprops={'edgecolor': 'white', 'linewidth': 0.5},
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_donut(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render a donut chart to base64 PNG."""
    _setup_style()

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct='%1.1f%%',
        colors=_CHART_COLORS[:len(labels)], startangle=90,
        wedgeprops={'edgecolor': 'white', 'linewidth': 0.5, 'width': 0.4},
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_scatter(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render a scatter chart to base64 PNG."""
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    x_positions = list(range(len(labels)))
    ax.scatter(x_positions, values, color='#2BB5A0', s=80, alpha=0.8, edgecolors='white', linewidth=0.5)
    ax.set_xticks(x_positions)
    ax.set_xticklabels(labels, rotation=30, fontsize=9)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')
    ax.tick_params(axis='y', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_horizontal_bar(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render a horizontal bar chart to base64 PNG. 长类别名 / 排名场景使用。"""
    _setup_style()

    # 倒序让最大 bar 在最上方
    rev_labels = list(reversed(labels))
    rev_values = list(reversed(values))

    fig, ax = plt.subplots(figsize=(8, max(4, len(labels) * 0.4)))
    colors = [_CHART_COLORS[i % len(_CHART_COLORS)] for i in range(len(rev_labels))]
    ax.barh(rev_labels, rev_values, color=colors, edgecolor='white', linewidth=0.5, height=0.7)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')
    ax.tick_params(axis='x', labelsize=9)
    ax.tick_params(axis='y', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='x', alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_chart(chart_type: str, title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """Render any chart type. Supports bar, grouped_bar, stacked_bar, line, area, pie, donut, scatter. Returns data URI string."""
    renderers = {
        'bar': render_bar,
        'grouped_bar': render_bar,
        'stacked_bar': render_bar,
        'horizontal_bar': render_horizontal_bar,
        'line': render_line,
        'area': render_area,
        'pie': render_pie,
        'donut': render_donut,
        'scatter': render_scatter,
    }
    fn = renderers.get(chart_type.lower())
    if fn is None:
        # unknown type → fallback to bar
        fn = render_bar
    return fn(title, labels, values, chart_config)
