import base64
import io
import matplotlib
matplotlib.use('Agg')  # 使用非交互式后端，避免 GUI 弹窗
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
from typing import Any


def _get_chinese_font():
    """查找系统中支持中文的字体。

    遍历候选字体列表，返回第一个系统中可用且支持中文的字体名；
    若均不可用，则返回 'sans-serif' 作为兜底。
    该函数用于解决 matplotlib 图表中文乱码问题。

    Returns:
        str: 可用的中文字体名称，或 'sans-serif'。
    """
    candidates = ['Microsoft YaHei', 'SimHei', 'SimSun', 'Noto Sans CJK SC', 'WenQuanYi Micro Hei']
    # 从系统字体列表中获取所有已注册字体的名称集合
    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            return name
    return 'sans-serif'


_CHART_COLORS = ['#2BB5A0', '#4A90D9', '#F5A623', '#D0021B', '#7B68EE', '#50C878', '#FF6B6B', '#45B7D1']
_PDF_DPI = 150


def _setup_style():
    """配置 matplotlib 的全局绘图样式。

    设置中文字体并关闭负号的 Unicode 显示问题。
    所有渲染函数在绘图前均需调用此函数。
    """
    font = _get_chinese_font()
    plt.rcParams['font.family'] = font
    plt.rcParams['axes.unicode_minus'] = False


def render_bar(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """渲染柱状图，返回 base64 编码的 PNG 图片 data URI。

    绘制纵向柱状图，每根柱子使用 _CHART_COLORS 中的颜色循环着色，
    隐藏顶部和右侧边框，x 轴标签旋转 30° 以避免重叠。

    Args:
        title: 图表标题
        labels: x 轴类别标签列表
        values: 每个类别对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    # 循环使用颜色列表，确保每个 bar 颜色不同
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
    """渲染折线图，返回 base64 编码的 PNG 图片 data URI。

    绘制带圆点标记的折线图，隐藏顶部和右侧边框，
    添加水平网格线以辅助阅读数据。

    Args:
        title: 图表标题
        labels: x 轴类别标签列表
        values: 每个类别对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
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
    """渲染面积图，返回 base64 编码的 PNG 图片 data URI。

    在折线图基础上使用 fill_between 填充下端区域，
    透明度设为 0.25 以保持视觉轻量，显示变化趋势及幅度。

    Args:
        title: 图表标题
        labels: x 轴类别标签列表
        values: 每个类别对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    # 用索引范围填充折线下方区域，因为 labels 是字符串不能直接传给 fill_between
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
    """渲染饼图，返回 base64 编码的 PNG 图片 data URI。

    绘制圆形饼图，自动计算百分比并标注在扇区上，
    从 90° 方向（正上方）开始顺时针排列。

    Args:
        title: 图表标题
        labels: 每个扇区的标签列表
        values: 每个扇区对应的数值列表（用于计算占比）
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct='%1.1f%%',
        colors=_CHART_COLORS[:len(labels)], startangle=90,
        wedgeprops={'edgecolor': 'white', 'linewidth': 0.5},
    )
    # 统一缩小百分比文字字号，避免拥挤
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_donut(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """渲染环形图（甜甜圈图），返回 base64 编码的 PNG 图片 data URI。

    与饼图类似，但通过 wedgeprops 的 width 参数在中心挖空，
    形成环形效果，视觉上更现代。

    Args:
        title: 图表标题
        labels: 每个扇区的标签列表
        values: 每个扇区对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    fig, ax = plt.subplots(figsize=(6, 6))
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct='%1.1f%%',
        colors=_CHART_COLORS[:len(labels)], startangle=90,
        # width=0.4 使饼图变成环形，中心空心区域占 60%
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
    """渲染散点图，返回 base64 编码的 PNG 图片 data URI。

    将 labels 映射为 x 轴等距位置，values 为 y 轴数值，
    每个点带白色描边，半透明填充，展示数据分布。

    Args:
        title: 图表标题
        labels: x 轴类别标签列表（映射到等距整数位置）
        values: 每个类别对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    fig, ax = plt.subplots(figsize=(8, 4))
    # 使用整数位置代替字符串标签作为 x 坐标
    x_positions = list(range(len(labels)))
    ax.scatter(x_positions, values, color='#2BB5A0', s=80, alpha=0.8, edgecolors='white', linewidth=0.5)
    # 手动设置 x 轴刻度位置和标签
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
    """渲染横向柱状图，返回 base64 编码的 PNG 图片 data URI。

    适合长类别名称或排名场景。数据会按 values 倒序排列，
    使最大值显示在最上方，图表高度根据类别数量动态调整。

    Args:
        title: 图表标题
        labels: y 轴类别标签列表
        values: 每个类别对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    # 倒序排列，让最大值 bar 显示在最上方，符合阅读习惯
    rev_labels = list(reversed(labels))
    rev_values = list(reversed(values))

    # 图表高度根据类别数量自适应，保证每行 bar 有足够空间
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


def render_radar(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """渲染雷达图，返回 base64 编码的 PNG 图片 data URI。

    用于多维评分/能力模型等场景。每个 label 对应一个雷达轴（维度），
    values 是该维度上的得分。数据会自动归一化到 0~1 范围。

    Args:
        title: 图表标题
        labels: 各维度名称列表（至少需要 3 个维度）
        values: 各维度得分列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    n = len(labels)
    if n < 3:
        # 雷达图至少需要 3 个维度才能形成闭合多边形，不足时退化为柱状图
        return render_bar(title, labels, values, chart_config)

    # 计算每个维度对应的角度（弧度），首尾闭合时最后一个点与第一个点重合
    angles = [n_ * 2 * np.pi / n for n_ in range(n)]
    angles_closed = angles + angles[:1]

    # 数值归一化到 0~1，便于多系列在同一雷达图上对比
    max_v = max(values) if values else 1
    max_v = max_v if max_v > 0 else 1  # 防止全零数据除零错误
    normalized = [(v / max_v) for v in values]
    normalized_closed = normalized + normalized[:1]

    fig, ax = plt.subplots(figsize=(7, 6), subplot_kw=dict(polar=True))
    ax.set_theta_offset(np.pi / 2)  # 将 0° 起点设在正上方（12 点钟方向）
    ax.set_theta_direction(-1)       # 角度方向设为顺时针
    ax.plot(angles_closed, normalized_closed, color=_CHART_COLORS[0], linewidth=2)
    ax.fill(angles_closed, normalized_closed, color=_CHART_COLORS[0], alpha=0.25)

    # 设置各维度的标签
    ax.set_xticks(angles)
    ax.set_xticklabels(labels, fontsize=10)

    # 半径刻度从 0 到 1，显示百分比格式
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(['25%', '50%', '75%', '100%'], fontsize=8, color='#8B97A8')
    ax.grid(color='#E2E8F0', linewidth=0.8)
    ax.spines['polar'].set_color('#E2E8F0')

    plt.title(title if title else '', fontsize=14, fontweight='bold', pad=18)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_heatmap(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """渲染热力图（兜底实现），返回 base64 编码的 PNG 图片 data URI。

    由于当前图表渲染接口仅支持一维 labels + values 的传参，
    无法完整实现二维热力图矩阵。此处作为兜底退化为柱状图展示。
    实际热力图渲染会在 PDF 生成阶段优先使用 _chartResult 的真实数据。

    Args:
        title: 图表标题
        labels: 类别标签列表
        values: 数值列表
        chart_config: 可选图表配置字典

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    return render_bar(title, labels, values, chart_config)


def render_funnel(title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """渲染漏斗图，返回 base64 编码的 PNG 图片 data URI。

    用一系列横向条形图模拟漏斗，条形的长度按 values 比例递减，
    适用于展示转化率/销售漏斗等场景。数值标注在条形右侧。

    Args:
        title: 图表标题
        labels: 各流程阶段名称列表
        values: 各阶段对应的数值列表
        chart_config: 可选图表配置字典（当前未使用，保留扩展接口）

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
    _setup_style()

    if not values:
        return render_bar(title, labels, values, chart_config)

    # 将 values 归一化到最宽 0.85，保证所有条形居中且留出边距
    max_v = max(values) if values else 1
    if max_v <= 0:
        max_v = 1
    widths = [v / max_v * 0.85 for v in values]

    # 图表高度根据阶段数量自适应
    fig, ax = plt.subplots(figsize=(8, max(4, len(labels) * 0.5)))
    y_positions = list(range(len(labels)))
    # 逐个绘制居中条形：left=(1-width)/2 使条形水平居中
    for i, (label, w, v) in enumerate(zip(labels, widths, values)):
        color = _CHART_COLORS[i % len(_CHART_COLORS)]
        x_start = (1 - w) / 2
        ax.barh(i, w, left=x_start, color=color, edgecolor='white', linewidth=0.5, height=0.7)
        # 在条形右侧标注原始数值，保留千分位格式
        ax.text(0.92, i, f'{v:,.0f}', va='center', ha='right', fontsize=9, color='#1A2332')

    ax.set_yticks(y_positions)
    ax.set_yticklabels(labels, fontsize=10)
    ax.set_xlim(0, 1)
    ax.set_xticks([])
    ax.invert_yaxis()  # 倒置 y 轴，让第一阶段显示在最上方
    ax.set_title(title if title else '', fontsize=14, fontweight='bold')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=_PDF_DPI, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode()}"


def render_chart(chart_type: str, title: str, labels: list[str], values: list[float], chart_config: dict[str, Any] | None = None) -> str:
    """通用图表渲染入口，根据 chart_type 分发到具体的渲染函数。

    支持以下图表类型：
        bar / grouped_bar / stacked_bar / horizontal_bar / line / area /
        pie / donut / scatter / radar / heatmap / funnel / kpi_card / sankey
    其中 group_bar、stacked_bar、kpi_card、sankey 目前暂用 bar 作为兜底实现。

    Args:
        chart_type: 图表类型字符串（不区分大小写）
        title: 图表标题
        labels: 类别标签列表
        values: 数值列表
        chart_config: 可选图表配置字典

    Returns:
        str: data:image/png;base64,... 格式的图片 URI
    """
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
        'radar': render_radar,
        'heatmap': render_heatmap,
        'funnel': render_funnel,
        'kpi_card': render_bar,
        'sankey': render_bar,
    }
    fn = renderers.get(chart_type.lower())
    if fn is None:
        # 遇到未知图表类型时，统一降级为柱状图
        fn = render_bar
    return fn(title, labels, values, chart_config)
