import ReactECharts from 'echarts-for-react'
import { DEFAULT_COLORS, formatFullNum } from './echartsUtils'

interface EChartsPieProps {
  data: Array<Record<string, unknown>>
  nameField: string
  valueField: string
  colors?: string[]
  innerRadius?: number | string
  title?: string
  /** ECharts 事件绑定（如点击扇区做联动筛选） */
  onEvents?: Record<string, (params: unknown) => void>
}

export default function EChartsPie({ data, nameField, valueField, colors = DEFAULT_COLORS, innerRadius, title, onEvents }: EChartsPieProps) {
  // 标题不画在图表内：块头已显示标题，再画一次是「双标题」（实测冗余观感）
  void title
  const radius = innerRadius !== undefined ? [String(innerRadius), '70%'] : '70%'
  const pieData = data.map(d => ({ name: String(d[nameField]), value: Number(d[valueField]) || 0 }))

  const option = {
    color: colors,
    tooltip: {
      trigger: 'item',
      confine: true,
      backgroundColor: 'rgba(255,255,255,0.98)',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: '#1E293B', fontSize: 12 },
      extraCssText: 'box-shadow: 0 6px 20px rgba(15,23,42,0.10); border-radius: 8px;',
      formatter: (p: { name: string; value: number; percent: number }) =>
        p.name + '<br/><b>' + formatFullNum(p.value) + '</b>（' + p.percent + '%）',
    },
    legend: {
      bottom: 0,
      type: 'scroll',
      icon: 'circle',
      itemWidth: 8,
      itemHeight: 8,
      itemGap: 12,
      textStyle: { color: '#475569', fontSize: 11 },
    },
    series: [{
      type: 'pie',
      radius,
      avoidLabelOverlap: true,
      // 白色描边把扇区分开，比纯色块干净
      itemStyle: { borderColor: '#FFFFFF', borderWidth: 2 },
      data: pieData,
      label: {
        show: pieData.length <= 8,
        color: '#475569',
        fontSize: 11,
        formatter: '{b} {d}%',
      },
      labelLine: { length: 8, length2: 8, lineStyle: { color: '#CBD5E1' } },
    }],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
}
