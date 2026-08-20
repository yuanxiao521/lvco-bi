import ReactECharts from 'echarts-for-react'
import { useMemo } from 'react'
import { DEFAULT_COLORS, type MeasureFieldInfo } from './echartsUtils'

interface EChartsRadarProps {
  data: Array<Record<string, unknown>>
  nameField: string
  /** 多度量：每个度量对应一个雷达图叠加 */
  measureFields: MeasureFieldInfo[]
  colors?: string[]
  title?: string
  onEvents?: Record<string, (params: unknown) => void>
}

/** 清理维度名称：去前后空格，去掉"市/省/区"后缀避免重复；空值返回 null */
function cleanName(raw: unknown): string | null {
  if (raw == null || raw === '') return null
  return String(raw).trim().replace(/[市省区]$/, '')
}

/** 雷达图最大维度数：超过会严重重叠，按第一度量值降序截取 Top N */
const MAX_RADAR_DIMENSIONS = 12

/**
 * 雷达图（多维度评分 / 竞品对比）
 * - 适用场景：能力模型评分、产品多维对比、KPI 平衡计分卡
 * - 维度 (indicator) 来自第一列（nameField）的唯一值
 * - 每个度量作为一个 series，半径 = 归一化后的值
 * - 自动归一化（min-max）避免量纲差异
 * - 维度过多时（>12）按首个度量值降序截取 Top 12，避免标签重叠
 */
export default function EChartsRadar({
  data,
  nameField,
  measureFields,
  colors = DEFAULT_COLORS,
  title,
  onEvents,
}: EChartsRadarProps) {
  // 防御：空数据直接显示占位
  if (!Array.isArray(data) || data.length === 0 || !measureFields || measureFields.length === 0) {
    return (
      <div className="w-full h-full flex items-center justify-center text-[12px] text-muted-foreground">
        暂无雷达图数据
      </div>
    )
  }

  const { indicator, series, truncated } = useMemo(() => {
    // 第一步：去重收集维度名（保留 null 用于"未填写"那一行）
    const cleanedToRow = new Map<string, Record<string, unknown>>()
    let nullRow: Record<string, unknown> | null = null

    for (const row of data) {
      const cleaned = cleanName(row[nameField])
      if (cleaned === null) {
        if (!nullRow) nullRow = row
        continue
      }
      if (!cleanedToRow.has(cleaned)) cleanedToRow.set(cleaned, row)
    }

    const realNames = Array.from(cleanedToRow.keys())
    if (realNames.length < 3) {
      return { indicator: [] as (string | null)[], series: [], truncated: false }
    }

    // 第二步：维度过多时，按首个度量值降序截取 Top N
    const firstField = measureFields[0]?.field
    let chosenNames = realNames
    let wasTruncated = false
    if (realNames.length > MAX_RADAR_DIMENSIONS && firstField) {
      const sorted = [...realNames].sort((a, b) => {
        const va = Number(cleanedToRow.get(a)?.[firstField]) || 0
        const vb = Number(cleanedToRow.get(b)?.[firstField]) || 0
        return vb - va
      })
      chosenNames = sorted.slice(0, MAX_RADAR_DIMENSIONS)
      wasTruncated = true
    }

    const indicator: (string | null)[] = [...chosenNames]
    if (nullRow) indicator.push(null)

    // 第三步：每个度量的 max
    const measureMaxes = measureFields.map((m) => {
      let max = 0
      for (const row of data) {
        const v = Number(row[m.field]) || 0
        if (v > max) max = v
      }
      return max || 1
    })

    const series = measureFields.map((m, i) => ({
      name: m.label,
      value: indicator.map((name) => {
        if (name === null) {
          const v = nullRow ? Number(nullRow[m.field]) || 0 : 0
          return Math.round((v / measureMaxes[i]) * 100)
        }
        const row = cleanedToRow.get(name)
        const v = row ? Number(row[m.field]) || 0 : 0
        return Math.round((v / measureMaxes[i]) * 100)
      }),
      areaStyle: { opacity: 0.18 },
      lineStyle: { width: 2 },
      itemStyle: { color: colors[i % colors.length] },
    }))

    return { indicator, series, truncated: wasTruncated }
  }, [data, nameField, measureFields, colors])

  if (indicator.length === 0) {
    return (
      <div className="w-full h-full flex items-center justify-center text-[12px] text-muted-foreground">
        雷达图至少需要 3 个维度，当前不足
      </div>
    )
  }

  // 雷达图维度多时缩小轴标签字号、缩短标签，避免重叠出框
  const dimCount = indicator.length
  const axisFontSize = dimCount > 8 ? 9 : dimCount > 6 ? 10 : 11
  const labelFormatter = (val: string) => (val && val.length > 6 ? `${val.slice(0, 6)}…` : val)

  const option = {
    title: title ? { text: title, left: 'center', textStyle: { fontSize: 13 } } : undefined,
    tooltip: { trigger: 'item' },
    legend: {
      bottom: 0,
      data: measureFields.map((m) => m.label),
      textStyle: { fontSize: 11 },
    },
    radar: {
      indicator: indicator.map((name) => ({
        name: name ?? 'null',
        max: 100,
      })),
      shape: 'polygon',
      splitNumber: 4,
      radius: '62%',
      center: ['50%', '46%'],
      axisName: {
        fontSize: axisFontSize,
        color: '#8B97A8',
        formatter: labelFormatter,
      },
      splitLine: { lineStyle: { color: '#E2E8F0' } },
      splitArea: { areaStyle: { color: ['#FFFFFF', '#F8FAFB'] } },
    },
    series: [
      {
        type: 'radar',
        data: series,
        symbol: 'circle',
        symbolSize: 4,
      },
    ],
  }
  return (
    <div className="w-full h-full flex flex-col overflow-hidden">
      <div className="flex-1 min-h-0">
        <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge onEvents={onEvents} />
      </div>
      {truncated ? (
        <div className="text-[10px] text-muted-foreground text-center pb-1">
          仅展示前 {MAX_RADAR_DIMENSIONS} 个维度（按首个度量值降序）
        </div>
      ) : null}
    </div>
  )
}
