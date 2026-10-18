import ReactECharts from 'echarts-for-react'
import { formatYAxisNum, formatFullNum, buildMultiMeasureOption, DEFAULT_COLORS } from './echartsUtils'
import type { MeasureFieldInfo } from './echartsUtils'

interface EChartsHorizontalBarProps {
  data: Array<Record<string, unknown>>
  xField: string
  yField?: string
  /** 多度量支持 */
  measureFields?: MeasureFieldInfo[]
  colors?: string[]
  color?: string
  title?: string
  /** 堆叠模式（把多个度量堆在同一行） */
  stacked?: boolean
  /** ECharts 事件绑定（如点击条形做联动筛选） */
  onEvents?: Record<string, (params: unknown) => void>
}

/**
 * 水平条形图（horizontal bar / 条形图）
 * - 类目在 Y 轴、数值在 X 轴，便于长类别名 / 排名场景
 * - 多度量时支持双 Y 轴 + PowerBI 风格图例
 * - 数据倒序绘制：TOP1 出现在最上方
 */
export default function EChartsHorizontalBar({
  data,
  xField,
  yField,
  measureFields,
  colors = DEFAULT_COLORS,
  color,
  title,
  stacked,
  onEvents,
}: EChartsHorizontalBarProps) {
  // 多度量模式：复用通用 builder（在 option 中标记 horizontal_bar=true 让 builder 生成水平条形）
  if (measureFields && measureFields.length > 0) {
    const option = buildMultiMeasureOption(
      'bar',
      data,
      xField,
      measureFields,
      colors,
      title,
      stacked,
      { horizontal: true },
    )
    return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
  }

  // 单度量模式（向后兼容）：y 轴为类目，x 轴为数值
  const y = yField ?? ''
  const cats = data.map(d => String(d[xField]))
  const vals = data.map(d => Number(d[y]) || 0)
  // 倒序：让最大的 bar 出现在最上方
  const reversedCats = [...cats].reverse()
  const reversedVals = [...vals].reverse()

  // 标题不画在图表内：块头已显示标题（避免双标题）
  void title
  const option = {
    tooltip: {
      trigger: 'axis',
      confine: true,
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(255,255,255,0.98)',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: '#1E293B', fontSize: 12 },
      extraCssText: 'box-shadow: 0 6px 20px rgba(15,23,42,0.10); border-radius: 8px;',
      valueFormatter: (v: number) => formatFullNum(v),
    },
    grid: { top: 12, left: 8, right: 44, bottom: 18, containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#94A3B8', fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#F1F5F9', width: 1 } },
    },
    yAxis: {
      type: 'category',
      data: reversedCats,
      axisLabel: { fontSize: 11, color: '#475569' },
      axisLine: { lineStyle: { color: '#E2E8F0' } },
      axisTick: { show: false },
    },
    series: [
      {
        name: y,
        type: 'bar',
        barMaxWidth: 26,
        data: reversedVals,
        itemStyle: { color: color || colors[0], borderRadius: [0, 6, 6, 0] },
        label: { show: true, position: 'right', fontSize: 10, color: '#94A3B8' },
      },
    ],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
}
