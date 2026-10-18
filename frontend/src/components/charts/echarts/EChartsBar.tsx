import ReactECharts from 'echarts-for-react'
import { formatYAxisNum, formatFullNum, buildMultiMeasureOption, DEFAULT_COLORS, sortByDateIfAll, formatCategoryLabel } from './echartsUtils'
import type { MeasureFieldInfo } from './echartsUtils'

interface EChartsBarProps {
  data: Array<Record<string, unknown>>
  xField: string
  yField?: string
  /** 多度量支持 */
  measureFields?: MeasureFieldInfo[]
  colors?: string[]
  color?: string
  title?: string
  /** 堆叠模式 */
  stacked?: boolean
  /** ECharts 事件绑定（如点击柱子做联动筛选） */
  onEvents?: Record<string, (params: unknown) => void>
}

export default function EChartsBar({ data, xField, yField, measureFields, colors = DEFAULT_COLORS, color, title, stacked, onEvents }: EChartsBarProps) {
  // 多度量模式
  if (measureFields && measureFields.length > 0) {
    const sorted = sortByDateIfAll(data, xField)
    const option = buildMultiMeasureOption('bar', sorted, xField, measureFields, colors, title, stacked)
    return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
  }

  // 单度量模式（向后兼容）
  const y = yField ?? ''
  const sorted = sortByDateIfAll(data, xField)
  const option = {
    tooltip: {
      trigger: 'axis',
      confine: true,
      backgroundColor: 'rgba(255,255,255,0.98)',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: '#1E293B', fontSize: 12 },
      extraCssText: 'box-shadow: 0 6px 20px rgba(15,23,42,0.10); border-radius: 8px;',
      valueFormatter: (v: number) => formatFullNum(v),
    },
    grid: { top: 14, left: 8, right: 16, bottom: 30, containLabel: true },
    xAxis: {
      type: 'category',
      data: sorted.map(d => String(d[xField])),
      axisLabel: {
        color: '#94A3B8',
        fontSize: 11,
        interval: 'auto',
        hideOverlap: true,
        formatter: (v: string) => formatCategoryLabel(v),
        rotate: sorted.length > 30 ? 45 : sorted.length > 6 ? 30 : 0,
      },
      axisLine: { lineStyle: { color: '#E2E8F0' } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v), color: '#94A3B8', fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: '#F1F5F9', width: 1 } },
    },
    series: [{
      type: 'bar',
      data: sorted.map(d => Number(d[y]) || 0),
      barMaxWidth: 36,
      itemStyle: { color: color || colors[0], borderRadius: [6, 6, 0, 0] },
    }],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
}
