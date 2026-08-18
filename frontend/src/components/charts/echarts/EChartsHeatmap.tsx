import ReactECharts from 'echarts-for-react'
import { useMemo, useRef, useEffect, useState } from 'react'

interface EChartsHeatmapProps {
  xFields: string[]
  yFields: string[]
  matrix: number[][]
  xLabel?: string
  yLabel?: string
  title?: string
  palette?: string[]
}

function formatNumber(v: number): string {
  if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(1) + '亿'
  if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(1) + '万'
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + 'k'
  if (Number.isInteger(v)) return String(v)
  return v.toFixed(1)
}

export default function EChartsHeatmap({
  xFields, yFields, matrix, xLabel, yLabel, title, palette,
}: EChartsHeatmapProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [, setTick] = useState(0)

  // 防御性归一化：避免 props 缺失或类型异常时崩溃
  const safeX = Array.isArray(xFields) ? xFields : []
  const safeY = Array.isArray(yFields) ? yFields : []
  const safeMatrix = Array.isArray(matrix) ? matrix : []

  // 监听容器大小变化，触发图表 resize
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setTick((t) => t + 1))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const data: [number, number, number][] = useMemo(() => {
    const out: [number, number, number][] = []
    for (let i = 0; i < safeY.length; i++) {
      for (let j = 0; j < safeX.length; j++) {
        const v = safeMatrix[i]?.[j]
        out.push([j, i, typeof v === 'number' && isFinite(v) ? v : 0])
      }
    }
    return out
  }, [safeX, safeY, safeMatrix])

  // 动态计算值域
  const { minVal, maxVal, isCorrelation } = useMemo(() => {
    let min = Infinity, max = -Infinity
    for (const row of safeMatrix) {
      if (!Array.isArray(row)) continue
      for (const raw of row) {
        const v = typeof raw === 'number' && isFinite(raw) ? raw : null
        if (v === null) continue
        if (v < min) min = v
        if (v > max) max = v
      }
    }
    if (!isFinite(min) || !isFinite(max)) { min = 0; max = 1 }
    if (min === max) { min = min - 1; max = max + 1 }
    const isCorr = min >= -1.01 && max <= 1.01
    return { minVal: min, maxVal: max, isCorrelation: isCorr }
  }, [safeMatrix])

  // 配色：优先使用传入的 palette，否则根据是否相关性选择
  const colorRange = useMemo(() => {
    if (palette && palette.length >= 2) return palette
    if (isCorrelation) return ['#3b82f6', '#ffffff', '#ef4444']
    return ['#f0f9ff', '#bae6fd', '#38bdf8', '#0ea5e9', '#0284c7']
  }, [palette, isCorrelation])

  // 空数据时直接显示占位，避免 ECharts 内部报错
  if (safeX.length === 0 || safeY.length === 0 || data.length === 0) {
    return (
      <div
        ref={containerRef}
        className="w-full h-full bg-white rounded-[10px] shadow-card p-2 flex items-center justify-center"
        style={{ minHeight: 280 }}
      >
        <div className="text-[13px] text-muted-foreground">暂无相关性数据</div>
      </div>
    )
  }

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
        const xName = safeX[x] ?? ''
        const yName = safeY[y] ?? ''
        return `<b>${yLabel || 'Y'}: ${yName}</b><br/><b>${xLabel || 'X'}: ${xName}</b><br/>${isCorrelation ? '相关系数' : '数值'}: <b>${formatNumber(v)}</b>`
      },
      backgroundColor: '#FFFFFF',
      borderColor: '#E2E8F0',
      borderWidth: 1,
      textStyle: { color: '#1A2332', fontSize: 12 },
    },
    grid: {
      top: title ? 50 : 30,
      left: 80,
      right: 60,
      bottom: 60,
      containLabel: false,
    },
    xAxis: {
      type: 'category' as const,
      data: safeX,
      position: 'top' as const,
      name: xLabel,
      nameLocation: 'middle' as const,
      nameGap: 22,
      nameTextStyle: { color: '#64748b', fontSize: 11 },
      splitArea: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        rotate: safeX.length > 5 ? 30 : 0,
        color: '#475569',
        fontSize: 11,
        interval: 0,
        formatter: (val: string) => (val.length > 10 ? val.slice(0, 10) + '…' : val),
      },
    },
    yAxis: {
      type: 'category' as const,
      data: safeY,
      name: yLabel,
      nameLocation: 'middle' as const,
      nameGap: 50,
      nameTextStyle: { color: '#64748b', fontSize: 11 },
      splitArea: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: '#475569',
        fontSize: 11,
        formatter: (val: string) => (val.length > 10 ? val.slice(0, 10) + '…' : val),
      },
    },
    visualMap: {
      min: minVal,
      max: maxVal,
      inRange: { color: colorRange },
      calculable: true,
      orient: 'horizontal' as const,
      left: 'center',
      bottom: 10,
      itemWidth: 12,
      itemHeight: 100,
      textStyle: { color: '#475569', fontSize: 11 },
      formatter: (v: number) => formatNumber(v),
    },
    series: [
      {
        type: 'heatmap' as const,
        data,
        itemStyle: { borderColor: '#FFFFFF', borderWidth: 1 },
        label: {
          show: safeX.length * safeY.length <= 144,
          formatter: (p: { value: [number, number, number] }) => formatNumber(p.value[2]),
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
      ref={containerRef}
      className="w-full bg-white rounded-[10px] shadow-card p-2"
      style={{ width: '100%', height: '100%', minHeight: 280 }}
    >
      <ReactECharts
        option={option}
        style={{ width: '100%', height: '100%' }}
        notMerge
      />
    </div>
  )
}
