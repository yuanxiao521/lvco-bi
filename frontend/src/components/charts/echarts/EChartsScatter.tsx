import ReactECharts from 'echarts-for-react'
import { formatYAxisNum, DEFAULT_COLORS } from './echartsUtils'

interface EChartsScatterProps {
  data: Array<Record<string, unknown>>
  xField: string
  yField?: string
  colors?: string[]
  color?: string
  title?: string
}

/**
 * 散点图（ECharts 实现）
 * - xField: 横轴字段（一般是数值或时间）
 * - yField: 纵轴字段（必填，散点图需要 2 个度量）
 * - 注：单 yField 模式用单 series；如有第二个度量可扩展为分组
 */
export default function EChartsScatter({
  data,
  xField,
  yField,
  colors = DEFAULT_COLORS,
  color,
  title,
}: EChartsScatterProps) {
  const y = yField ?? ''
  const points = data.map(d => [Number(d[xField]) || 0, Number(d[y]) || 0])

  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: {
      trigger: 'item',
      formatter: (params: { value: number[] }) =>
        `${xField}: ${params.value[0]}<br/>${y}: ${params.value[1]}`,
    },
    xAxis: {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v) },
      scale: true,
    },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: (v: number) => formatYAxisNum(v) },
      scale: true,
    },
    series: [
      {
        type: 'scatter',
        data: points,
        symbolSize: 10,
        itemStyle: {
          color: color || colors[0],
          opacity: 0.75,
        },
      },
    ],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
}