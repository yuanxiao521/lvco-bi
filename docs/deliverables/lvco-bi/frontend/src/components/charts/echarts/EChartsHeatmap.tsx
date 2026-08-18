import ReactECharts from 'echarts-for-react'
import { useMemo } from 'react'

interface EChartsHeatmapProps {
  fields: string[]
  matrix: number[][]
  title?: string
}

export default function EChartsHeatmap({ fields, matrix, title }: EChartsHeatmapProps) {
  const n = fields.length
  // 根据字段数动态计算高度,避免父容器高度为 0 时图表不可见
  const cellSize = n <= 5 ? 70 : n <= 10 ? 50 : n <= 15 ? 38 : 28
  const totalWidth = Math.max(560, cellSize * n + 180)
  const totalHeight = Math.max(420, cellSize * n + 160)

  const data: [number, number, number][] = useMemo(() => {
    const out: [number, number, number][] = []
    for (let i = 0; i < n; i++) {
      for (let j = 0; j < n; j++) {
        out.push([j, i, Math.round((matrix[i]?.[j] ?? 0) * 100) / 100])
      }
    }
    return out
  }, [fields, matrix, n])

  const option = {
    title: title
      ? {
          text: title,
          left: 'center',
          top: 8,
          textStyle: { fontSize: 13, fontWeight: 600, color: '#1A2332' },
        }
      : undefined,
    tooltip: {
      formatter: (params: { value: [number, number, number] }) => {
        const [x, y, v] = params.value
        return `<b>${fields[y]}</b> × <b>${fields[x]}</b><br/>相关系数: <b>${v.toFixed(3)}</b>`
      },
      backgroundColor: '#FFFFFF',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      textStyle: { color: '#1A2332', fontSize: 12 },
    },
    grid: {
      top: title ? 50 : 30,
      left: Math.min(180, cellSize * 1.8),
      right: 60,
      bottom: 90,
      containLabel: false,
    },
    xAxis: {
      type: 'category' as const,
      data: fields,
      position: 'top' as const,
      splitArea: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        rotate: 45,
        color: '#475569',
        fontSize: 11,
        interval: 0,
        formatter: (val: string) => (val.length > 12 ? val.slice(0, 12) + '…' : val),
      },
    },
    yAxis: {
      type: 'category' as const,
      data: fields,
      splitArea: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: '#475569',
        fontSize: 11,
        formatter: (val: string) => (val.length > 12 ? val.slice(0, 12) + '…' : val),
      },
    },
    visualMap: {
      min: -1,
      max: 1,
      inRange: { color: ['#3b82f6', '#ffffff', '#ef4444'] },
      calculable: true,
      orient: 'horizontal' as const,
      left: 'center',
      bottom: 36,
      itemWidth: 12,
      itemHeight: 120,
      textStyle: { color: '#475569', fontSize: 11 },
    },
    series: [
      {
        type: 'heatmap' as const,
        data,
        // 根据字段数动态调整单元格大小
        itemStyle: { borderColor: '#FFFFFF', borderWidth: 1 },
        label: {
          show: n <= 12,
          formatter: (p: { value: [number, number, number] }) => p.value[2].toFixed(2),
          // 根据相关性强度自动反色,白底深字、红/蓝底白字都清晰
          color: '#1A2332',
          textBorderColor: 'transparent',
          fontSize: 11,
          fontWeight: 500,
        },
        emphasis: {
          itemStyle: {
            shadowBlur: 8,
            shadowColor: 'rgba(0,0,0,0.25)',
          },
        },
      },
    ],
  }

  return (
    <div
      className="bg-white rounded-[10px] shadow-card p-4 overflow-auto"
      style={{ minHeight: 420 }}
    >
      <ReactECharts
        option={option}
        style={{ height: totalHeight, width: totalWidth }}
        notMerge
      />
    </div>
  )
}
