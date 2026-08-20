import ReactECharts from 'echarts-for-react'
import { DEFAULT_COLORS } from './echartsUtils'

interface SankeyLink {
  source: string
  target: string
  value: number
}

interface EChartsSankeyProps {
  data: Array<Record<string, unknown>>
  /** 源节点的字段名（第一列维度） */
  sourceField: string
  /** 目标节点的字段名（第二列维度，可选，无则用固定名称） */
  targetField?: string
  /** 值字段 */
  valueField: string
  colors?: string[]
  title?: string
  onEvents?: Record<string, (params: unknown) => void>
}

/**
 * 桑基图（流向分析专用）
 * - 适用场景：资金流向、用户流转、流量分配
 * - 数据格式：每行 = [source, target, value]
 */
export default function EChartsSankey({
  data,
  sourceField,
  targetField,
  valueField,
  colors = DEFAULT_COLORS,
  title,
  onEvents,
}: EChartsSankeyProps) {
  // 构建节点和链接
  const nodeSet = new Set<string>()
  const links: SankeyLink[] = []

  for (const row of data) {
    const source = String(row[sourceField] ?? '')
    const target = targetField
      ? String(row[targetField] ?? '')
      : String(row[Object.keys(row).find(k => k !== sourceField) ?? ''] ?? '')
    const value = Number(row[valueField]) || 0

    if (!source || !target) continue
    nodeSet.add(source)
    nodeSet.add(target)
    links.push({ source, target, value })
  }

  const nodes = Array.from(nodeSet).map((name, i) => ({
    name,
    itemStyle: { color: colors[i % colors.length] },
  }))

  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: {
      trigger: 'item',
      triggerOn: 'mousemove',
    },
    series: [
      {
        type: 'sankey',
        layout: 'none',
        emphasis: { focus: 'adjacency' },
        nodeAlign: 'left',
        data: nodes,
        links: links.map(l => ({
          source: l.source,
          target: l.target,
          value: l.value,
        })),
        label: {
          show: true,
          fontSize: 11,
        },
        lineStyle: {
          color: 'gradient',
          curveness: 0.5,
        },
      },
    ],
  }

  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
}
