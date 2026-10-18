import ReactECharts from 'echarts-for-react'
import { formatYAxisNum, formatFullNum, buildMultiMeasureOption, DEFAULT_COLORS, sortByDateIfAll } from './echartsUtils'
import type { MeasureFieldInfo } from './echartsUtils'

interface EChartsAreaProps {
  data: Array<Record<string, unknown>>
  xField: string
  yField?: string
  /** 多度量支持 */
  measureFields?: MeasureFieldInfo[]
  colors?: string[]
  color?: string
  title?: string
  /** ECharts 事件绑定（如点击节点做联动筛选） */
  onEvents?: Record<string, (params: unknown) => void>
}

export default function EChartsArea({ data, xField, yField, measureFields, colors = DEFAULT_COLORS, color, title, onEvents }: EChartsAreaProps) {
  // 多度量模式
  if (measureFields && measureFields.length > 0) {
    const option = buildMultiMeasureOption('area', data, xField, measureFields, colors, title)
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
      type: 'line',
      data: sorted.map(d => Number(d[y]) || 0),
      smooth: 0.35,
      lineStyle: { width: 2.5 },
      areaStyle: { opacity: 0.16 },
      itemStyle: { color: color || colors[0] },
    }],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
}
