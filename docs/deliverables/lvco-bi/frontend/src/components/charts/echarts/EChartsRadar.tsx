import ReactECharts from 'echarts-for-react'
import { DEFAULT_COLORS, type MeasureFieldInfo } from './echartsUtils'

interface EChartsRadarProps {
  data: Array<Record<string, unknown>>
  nameField: string
  /** 多度量：每个度量对应一个雷达图叠加 */
  measureFields: MeasureFieldInfo[]
  colors?: string[]
  title?: string
}

/**
 * 雷达图（多维度评分 / 竞品对比）
 * - 适用场景：能力模型评分、产品多维对比、KPI 平衡计分卡
 * - 维度 (indicator) 来自第一列（nameField）的唯一值
 * - 每个度量作为一个 series，半径 = 归一化后的值
 * - 自动归一化（min-max）避免量纲差异
 */
export default function EChartsRadar({
  data,
  nameField,
  measureFields,
  colors = DEFAULT_COLORS,
  title,
}: EChartsRadarProps) {
  // 维度列表（来自 nameField 的唯一值，按出现顺序）
  const indicator: string[] = []
  const seen = new Set<string>()
  for (const row of data) {
    const name = String(row[nameField])
    if (!seen.has(name)) {
      seen.add(name)
      indicator.push(name)
    }
  }

  // 计算每个度量的 max（用于 indicator.max，归一化到 0-100）
  const measureMaxes = measureFields.map((m) => {
    let max = 0
    for (const row of data) {
      const v = Number(row[m.field]) || 0
      if (v > max) max = v
    }
    return max || 1
  })

  // 每个 series 的值
  const series = measureFields.map((m, i) => ({
    name: m.label,
    value: indicator.map((name) => {
      const row = data.find((d) => String(d[nameField]) === name)
      const raw = row ? Number(row[m.field]) || 0 : 0
      // 归一化到 0~100
      return Math.round((raw / measureMaxes[i]) * 100)
    }),
    areaStyle: { opacity: 0.18 },
    lineStyle: { width: 2 },
    itemStyle: { color: colors[i % colors.length] },
  }))

  const option = {
    title: title ? { text: title, left: 'center' } : undefined,
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, data: measureFields.map((m) => m.label) },
    radar: {
      indicator: indicator.map((name) => ({
        name,
        // ECharts 要求所有维度同 scale，归一化到 0~100
        max: 100,
      })),
      shape: 'polygon',
      splitNumber: 4,
      axisName: { fontSize: 11, color: '#8B97A8' },
      splitLine: { lineStyle: { color: '#E2E8F0' } },
      splitArea: { areaStyle: { color: ['#FFFFFF', '#F8FAFB'] } },
    },
    series: [
      {
        type: 'radar',
        data: series,
      },
    ],
  }
  return <ReactECharts option={option} style={{ height: '100%', width: '100%' }} notMerge />
}