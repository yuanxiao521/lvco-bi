import ReactECharts from 'echarts-for-react'
import { formatYAxisNum, buildMultiMeasureOption, DEFAULT_COLORS } from './echartsUtils'
import type { MeasureFieldInfo } from './echartsUtils'

interface EChartsLineProps {
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

export default function EChartsLine({ data, xField, yField, measureFields, colors = DEFAULT_COLORS, color, title, onEvents }: EChartsLineProps) {
  // 多度量模式
  if (measureFields && measureFields.length > 0) {
    const option = buildMultiMeasureOption('line', data, xField, measureFields, colors, title)
    return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
  }

  // 单度量模式（向后兼容）
  const y = yField ?? ''
  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: data.map(d => String(d[xField])) },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => formatYAxisNum(v) } },
    series: [{
      type: 'line',
      data: data.map(d => Number(d[y]) || 0),
      smooth: true,
      itemStyle: { color: color || colors[0] },
    }],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
}
