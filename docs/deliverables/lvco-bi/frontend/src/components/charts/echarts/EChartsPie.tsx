import ReactECharts from 'echarts-for-react'

interface EChartsPieProps {
  data: Array<Record<string, unknown>>
  nameField: string
  valueField: string
  colors?: string[]
  innerRadius?: number | string
  title?: string
}

const DEFAULT_COLORS = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4']

export default function EChartsPie({ data, nameField, valueField, colors = DEFAULT_COLORS, innerRadius, title }: EChartsPieProps) {
  const radius = innerRadius !== undefined ? [String(innerRadius), '70%'] : '70%'
  const pieData = data.map(d => ({ name: String(d[nameField]), value: Number(d[valueField]) || 0 }))

  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: { trigger: 'item' },
    legend: { bottom: 0 },
    series: [{
      type: 'pie',
      radius,
      data: pieData,
      itemStyle: {
        color: (params: { dataIndex: number }) => colors[params.dataIndex % colors.length],
      },
      label: { show: pieData.length <= 6 },
    }],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
}
