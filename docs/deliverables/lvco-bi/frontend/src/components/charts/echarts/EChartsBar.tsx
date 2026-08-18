import ReactECharts from 'echarts-for-react'
import { formatYAxisNum, buildMultiMeasureOption, DEFAULT_COLORS } from './echartsUtils'
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
}

export default function EChartsBar({ data, xField, yField, measureFields, colors = DEFAULT_COLORS, color, title, stacked }: EChartsBarProps) {
  // 多度量模式
  if (measureFields && measureFields.length > 0) {
    const option = buildMultiMeasureOption('bar', data, xField, measureFields, colors, title, stacked)
    return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
  }

  // 单度量模式（向后兼容）
  const y = yField ?? ''
  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: data.map(d => String(d[xField])) },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => formatYAxisNum(v) } },
    series: [{
      type: 'bar',
      data: data.map(d => Number(d[y]) || 0),
      itemStyle: { color: color || colors[0] },
    }],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
}
