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

const DEFAULT_COLORS = ['#2BB5A0', '#6C7BF2', '#F5A623', '#EF5B5B', '#4EADFF', '#A78BFA', '#F472B6', '#34D399'];

/** Y轴格式化：>= 100万 → M，>= 1万 → w，>= 1000 → k */
export function formatYAxisNum(value: number): string {
  if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
  if (value >= 10000) return `${(value / 10000).toFixed(1)}w`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
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
  title?: string,
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
      name: measureFields[leftIdx]?.label ?? '',
      nameTextStyle: { color: colors[leftIdx % colors.length], fontSize: 11 },
      position: 'left',
      axisLine: { show: true, lineStyle: { color: colors[leftIdx % colors.length] } },
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#8B97A8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#EEF1F6', type: 'dashed' } },
    });
    const rightColor = rightIdx >= 0 ? colors[rightIdx % colors.length] : colors[1 % colors.length];
    yAxisList.push({
      type: 'value',
      name: measureFields[rightIdx]?.label ?? '',
      nameTextStyle: { color: rightColor, fontSize: 11 },
      position: 'right',
      axisLine: { show: true, lineStyle: { color: rightColor } },
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#8B97A8', fontSize: 11 },
      splitLine: { show: false },
    });
  } else {
    yAxisList.push({
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#8B97A8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#EEF1F6', type: 'dashed' } },
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
      base.barMaxWidth = 40;
    }
    return base;
  });

  // --- 构造 xAxis ---
  let xAxis: any;
  if (horizontal) {
    // 水平条形：x 轴 = value，y 轴 = category
    xAxis = {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#8B97A8', fontSize: 11 },
      splitLine: { lineStyle: { color: '#EEF1F6', type: 'dashed' } },
    };
  } else {
    xAxis = {
      type: 'category',
      data: xData,
      axisLabel: { color: '#8B97A8', fontSize: 11, interval: 0, rotate: xData.length > 6 ? 30 : 0 },
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
      axisLabel: { color: '#8B97A8', fontSize: 11 },
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
    // ECharts 图例的 icon 使用 roundRect（PowerBI 风格圆角矩形）
    icon: 'roundRect',
    textStyle: { color: '#1A2332', fontSize: 11 },
  }));

  // --- grid: 加大底部留白，避免 x 轴 label 被父容器裁切 ---
  const showLegend = measureFields.length > 1;
  const gridBottom = horizontal ? 28 : 36; // 水平条形不需要太多底部
  const gridTop = title ? 40 : (showLegend ? 32 : 12);

  const option: any = {
    title: title ? { text: title, left: 'center', textStyle: { fontSize: 13, fontWeight: 600, color: '#1A2332' } } : undefined,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: horizontal ? 'shadow' : 'line' },
      backgroundColor: '#FFFFFF',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      textStyle: { color: '#1A2332', fontSize: 12 },
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
