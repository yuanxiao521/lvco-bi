/**
 * ECharts 共享工具：Y轴格式化、多度量图表 option 构建
 *
 * 本次重构（修复双Y轴 + 内联截断 + PowerBI 风格图例）：
 * 1. 双 Y 轴：measureFields.length >= 2 时强制开启，非最大值度量自动分配到右轴
 * 2. 右轴必须 position:'right'，并配套 axisLine/axisLabel 颜色与 series 对应
 * 3. grid.bottom 加大到 28，确保 x 轴 label 不会被父容器底部裁切
 * 4. legend 强化：可点击切换（selectedMode）、可滚动、可按 series 类型分组显示
 * 5. 支持水平条形（horizontal=true）模式：x/y 轴对调
 */

const DEFAULT_COLORS = ['#3E7BFA', '#14B8A6', '#F59E0B', '#EF4444', '#8B5CF6', '#0EA5E9', '#EC4899', '#22C55E'];

/** 数值格式化：中文量纲（亿/万/千），整数不带小数点——比 M/w/k 更符合中文阅读习惯 */
export function formatYAxisNum(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  const trim = (v: number) => (Math.abs(v - Math.round(v)) < 0.05 ? String(Math.round(v)) : v.toFixed(1));
  if (abs >= 100000000) return `${sign}${trim(abs / 100000000)}亿`;
  if (abs >= 10000) return `${sign}${trim(abs / 10000)}万`;
  if (abs >= 1000) return `${sign}${trim(abs / 1000)}千`;
  return sign + trim(abs);
}

/** 千分位格式化（tooltip 用） */
export function formatFullNum(value: number): string {
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

export interface MeasureFieldInfo {
  field: string;   // 列名（如 "sum_sales"）
  label: string;   // 显示标签（如 "SUM(sales)"）
}

export interface BuildOptionExtra {
  /** 水平条形图：x/y 轴对调，类目在纵轴 */
  horizontal?: boolean;
  /** 图例位置：'top' | 'bottom'，默认 'top' */
  legendPosition?: 'top' | 'bottom';
  /** 图例额外显示系列类型分组（PowerBI 风格分层） */
  legendGroup?: string;
}

/**
 * 判断哪些度量用右轴：
 * - 双 Y 轴开启（measureFields.length >= 2）
 * - 把"非最大值"度量分配到右轴（避免小值被大值压成一条直线）
 * - 至少保留一个度量在左轴
 */
function decideAxisIndex(maxes: number[], useDualAxis: boolean): number[] {
  const result: number[] = new Array(maxes.length).fill(0);
  if (!useDualAxis) return result;
  const globalMax = Math.max(...maxes);
  // 把"明显小于最大值的"分配到右轴
  maxes.forEach((m, i) => {
    if (m > 0 && m < globalMax) result[i] = 1;
  });
  // 至少保证左轴有一个度量，否则强制第一个为 0
  if (!result.includes(0) && result.length > 0) result[0] = 0;
  return result;
}

/**
 * 构建多度量 ECharts option
 * - chartType: 'bar' | 'line' | 'area'
 * - extra.horizontal=true 时：水平条形（x/y 对调，类目在 Y 轴）
 */
export function buildMultiMeasureOption(
  chartType: 'bar' | 'line' | 'area',
  data: Array<Record<string, unknown>>,
  xField: string,
  measureFields: MeasureFieldInfo[],
  colors: string[] = DEFAULT_COLORS,
  _title?: string,
  stacked?: boolean,
  extra: BuildOptionExtra = {},
) {
  const xData = data.map((d) => String(d[xField]));
  const useDualAxis = measureFields.length >= 2; // 双 Y 轴：≥2 度量强制开启
  const isArea = chartType === 'area';
  const horizontal = !!extra.horizontal;
  const legendPos = extra.legendPosition ?? 'top';

  // 计算每个度量的最大值，用于右轴分配
  const maxes = measureFields.map((f) => {
    let max = 0;
    for (const row of data) {
      const v = Number(row[f.field]) || 0;
      if (v > max) max = v;
    }
    return max;
  });
  const axisAssignments = decideAxisIndex(maxes, useDualAxis);

  // --- 构造 yAxis 数组 ---
  // 左轴（index 0）：主度量
  // 右轴（index 1）：次度量
  const yAxisList: any[] = [];
  if (useDualAxis) {
    // 关键修复：name 必须绑到对应轴上绑定的度量，而不是 measureFields[0]
    const leftIdx = axisAssignments.findIndex((a) => a === 0);
    const rightIdx = axisAssignments.findIndex((a) => a === 1);
    yAxisList.push({
      type: 'value',
      // 不设 name：轴名（如 SUM(total_amount)）会画在轴顶端并与 legend 重叠，
      // 遮挡顶部刻度（实测）。左右轴的归属靠 axisLine 颜色 + legend 颜色对应即可。
      position: 'left',
      axisLine: { show: true, lineStyle: { color: colors[leftIdx % colors.length], width: 1 } },
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#94A3B8', fontSize: 11 },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#F1F5F9', width: 1 } },
    });
    const rightColor = rightIdx >= 0 ? colors[rightIdx % colors.length] : colors[1 % colors.length];
    yAxisList.push({
      type: 'value',
      position: 'right',
      axisLine: { show: true, lineStyle: { color: rightColor, width: 1 } },
      axisTick: { show: false },
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#94A3B8', fontSize: 11 },
      splitLine: { show: false },
    });
  } else {
    yAxisList.push({
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#94A3B8', fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#F1F5F9', width: 1 } },
    });
  }

  // --- 构造 series ---
  const series = measureFields.map((m, i) => {
    const base: any = {
      name: m.label,
      type: isArea ? 'line' : chartType,
      data: data.map((d) => Number(d[m.field]) || 0),
      itemStyle: { color: colors[i % colors.length] },
      yAxisIndex: axisAssignments[i],
    };
    if (stacked && chartType === 'bar') {
      base.stack = 'total';
    }
    if (chartType === 'line' || isArea) {
      base.smooth = true;
      base.symbol = 'circle';
      base.symbolSize = 6;
    }
    if (isArea) {
      base.areaStyle = { opacity: 0.25 };
    }
    if (chartType === 'bar') {
      base.barMaxWidth = 36;
      // 圆角柱：比直角柱更现代（横向柱圆角在右侧）
      base.itemStyle = {
        ...base.itemStyle,
        borderRadius: horizontal ? [0, 6, 6, 0] : [6, 6, 0, 0],
      };
    }
    if (chartType === 'line' || isArea) {
      base.lineStyle = { width: 2.5 };
    }
    return base;
  });

  // --- 构造 xAxis ---
  let xAxis: any;
  if (horizontal) {
    // 水平条形：x 轴 = value，y 轴 = category
    xAxis = {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#94A3B8', fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#F1F5F9', width: 1 } },
    };
  } else {
    xAxis = {
      type: 'category',
      data: xData,
      axisLabel: {
        color: '#94A3B8',
        fontSize: 11,
        // 少量类目全显 + 轻度倾斜；多类目（如按日趋势 100+ 点）交给 ECharts
        // 自动抽稀 + hideOverlap，绝不全量渲染——此前 interval:0 会把日期轴糊成一团。
        interval: 'auto',
        hideOverlap: true,
        formatter: (v: string) => formatCategoryLabel(v),
        rotate: xData.length > 30 ? 45 : xData.length > 6 ? 30 : 0,
      },
      axisLine: { lineStyle: { color: '#E2E8F0' } },
    };
  }

  // --- 构造 yAxis 输出 ---
  let yAxisOut: any;
  if (horizontal) {
    // 水平条形：类目在 Y 轴（倒序让 TOP1 在最上方）
    yAxisOut = {
      type: 'category',
      data: [...xData].reverse(),
      axisLabel: { color: '#94A3B8', fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
    };
  } else if (useDualAxis) {
    yAxisOut = yAxisList;
  } else {
    yAxisOut = yAxisList[0];
  }

  // --- PowerBI 风格 legend ---
  // 每个 series 都可点击切换显示/隐藏
  const legendData = measureFields.map((m) => ({
    name: m.label,
    // 圆点图例：比圆角矩形更轻，不与柱/线抢视觉
    icon: 'circle',
    textStyle: { color: '#475569', fontSize: 11 },
  }));

  // --- grid: 加大底部留白，避免 x 轴 label 被父容器裁切 ---
  const showLegend = measureFields.length > 1;
  const gridBottom = horizontal ? 24 : 34; // 水平条形不需要太多底部
  // 标题不再画在图表内（块头已显示），顶部只需给图例留位
  const gridTop = showLegend ? 30 : 10;

  const option: any = {
    // 不画内置标题：块头已经显示标题，再画一次就是"双标题"（实测冗余观感）
    title: undefined,
    tooltip: {
      trigger: 'axis',
      confine: true,
      axisPointer: { type: horizontal ? 'shadow' : 'line', lineStyle: { color: '#CBD5E1', width: 1 } },
      backgroundColor: 'rgba(255,255,255,0.98)',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: '#1E293B', fontSize: 12 },
      extraCssText: 'box-shadow: 0 6px 20px rgba(15,23,42,0.10); border-radius: 8px;',
      valueFormatter: (v: number) => formatFullNum(v),
    },
    legend: {
      show: showLegend,
      data: legendData,
      [legendPos]: legendPos === 'top' ? 6 : 4,
      left: 'center',
      // PowerBI 风格：可点击切换、可滚动
      selectedMode: 'multiple',
      type: 'scroll',
      pageIconColor: '#8B97A8',
      pageTextStyle: { color: '#8B97A8' },
      itemWidth: 12,
      itemHeight: 8,
      itemGap: 14,
    },
    grid: {
      top: gridTop,
      left: 8,
      right: useDualAxis ? 12 : 16,
      bottom: gridBottom,
      containLabel: true,
    },
    xAxis,
    yAxis: yAxisOut,
    series,
  };

  return option;
}

export { DEFAULT_COLORS };


/** 判断类目值是否像日期（ISO 日期/月/时间串） */
function looksLikeDate(v: unknown): boolean {
  const t = String(v ?? '');
  return /^\d{4}-\d{2}(-\d{2})?/.test(t) || /^\d{4}\/\d{1,2}/.test(t);
}

/**
 * 日期类目排序：数据库 GROUP BY 不保证顺序（实测「按日期趋势」X 轴乱跳）。
 * 仅当**全部**类目都像日期时按字符串升序稳定排序（ISO 串字典序 = 时间序），
 * 其余情况保持原顺序，避免打乱「按销售额排名」这类有业务语义的顺序。
 */
export function sortByDateIfAll<T extends Record<string, unknown>>(data: T[], xField: string): T[] {
  if (!Array.isArray(data) || data.length < 2) return data;
  if (!data.every((d) => looksLikeDate(d[xField]))) return data;
  return [...data].sort((a, b) => String(a[xField] ?? '').localeCompare(String(b[xField] ?? '')));
}


/**
 * 类目轴短标签：日期时间戳裁掉时间部分（date_trunc 常返回 '2023-01-01 00:00:00'），
 * 长 ISO 串在 X 轴上非常占位且不易读。
 */
export function formatCategoryLabel(v: unknown): string {
  const t = String(v ?? '');
  if (/^\d{4}-\d{2}-\d{2} 00:00:00$/.test(t)) return t.slice(0, 7);
  const m = t.match(/^(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}/);
  return m ? m[1] : t;
}
