import ReactECharts from 'echarts-for-react'
import { DEFAULT_COLORS } from './echartsUtils'

interface EChartsFunnelProps {
  data: Array<Record<string, unknown>>
  nameField: string
  valueField: string
  colors?: string[]
  title?: string
}

/**
 * 漏斗图（转化率分析专用）
 * - 适用场景：销售漏斗、注册转化、用户旅程
 * - 维度按 value 降序自动排列（漏斗语义）
 * - 标签内嵌显示转化率（百分比），便于快速读懂流失
 */
export default function EChartsFunnel({
  data,
  nameField,
  valueField,
  colors = DEFAULT_COLORS,
  title,
}: EChartsFunnelProps) {
  // 按 value 降序排序（漏斗宽 → 漏斗窄）
  const sortedData = [...data].sort(
    (a, b) => (Number(b[valueField]) || 0) - (Number(a[valueField]) || 0),
  )
  const funnelData = sortedData.map(d => ({
    name: String(d[nameField]),
    value: Number(d[valueField]) || 0,
  }))

  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c}<br/>占比 {d}%',
    },
    legend: { bottom: 0, type: 'scroll' },
    series: [
      {
        type: 'funnel',
        left: '10%',
        right: '10%',
        top: 30,
        bottom: 40,
        sort: 'descending', // 漏斗语义：宽→窄
        gap: 2,
        label: {
          show: true,
          position: 'inside',
          formatter: '{b}: {c} ({d}%)',
        },
        labelLine: { show: false },
        itemStyle: {
          borderColor: '#fff',
          borderWidth: 1,
          color: (params: { dataIndex: number }) =>
            colors[params.dataIndex % colors.length],
        },
        data: funnelData,
      },
    ],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
}