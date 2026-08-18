import { useEffect, useState } from 'react'
import {
  BarChart3,
  Loader2,
  Database,
  Hash,
  Key,
  Calendar,
  TrendingUp,
  Lightbulb,
  AlertCircle,
  Info,
  Activity,
  ArrowUp,
  Gauge,
  Sparkles,
  RefreshCw,
  Wand2,
  Trash2,
  CheckCircle2,
} from 'lucide-react'
import {
  describeStatistics,
  correlationMatrix,
  getSummary,
  getPreview,
} from '../../api/statistics'
import type { SummaryResult, PreviewResult } from '../../api/statistics'
import { listDatasources, getDatasource, aiCleanDatasource } from '../../api/datasources'
import { generateInsights, applyClean } from '../../api/ai'
import EChartsHeatmap from '../../components/charts/echarts/EChartsHeatmap'
import type { DataSource, SchemaField } from '../../api/types'

interface StatItem {
  field: string
  count: number
  mean: number | null
  std: number | null
  min: number | null
  p25: number | null
  p50: number | null
  p75: number | null
  max: number | null
  null_count: number
  null_rate: number
}

function formatNum(val: number | null): string {
  if (val === null || val === undefined) return '—'
  return val.toFixed(2)
}

function formatRate(val: number): string {
  return (val * 100).toFixed(1) + '%'
}

function formatCount(n: number): string {
  return n.toLocaleString('zh-CN')
}

function DescribeTable({ stats }: { stats: StatItem[] }) {
  if (stats.length === 0) {
    return (
      <div className="py-10 text-center text-[13px] text-muted-foreground">
        该数据源没有可统计的数值字段
      </div>
    )
  }

  return (
    <div className="overflow-x-auto border border-border-light rounded-md">
      <table className="w-full text-[13px] min-w-[700px]">
        <thead>
          <tr className="bg-muted">
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">字段</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Count</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Mean</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Std</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Min</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">P25</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">P50</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">P75</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Max</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Null Count</th>
            <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">Null Rate</th>
          </tr>
        </thead>
        <tbody>
          {stats.map((s) => {
            let rowClass = ''
            if (s.null_rate > 0.3) {
              rowClass = 'bg-red-50'
            } else if (s.null_rate > 0.1) {
              rowClass = 'bg-yellow-50'
            }

            return (
              <tr key={s.field} className={`border-t border-border-light ${rowClass}`}>
                <td className="px-3 py-2 font-medium text-foreground">{s.field}</td>
                <td className="px-3 py-2 text-card-foreground">{s.count}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.mean)}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.std)}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.min)}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.p25)}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.p50)}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.p75)}</td>
                <td className="px-3 py-2 text-card-foreground">{formatNum(s.max)}</td>
                <td className="px-3 py-2 text-card-foreground">{s.null_count}</td>
                <td className="px-3 py-2 text-card-foreground">{formatRate(s.null_rate)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 从描述性统计结果中自动提取洞察摘要 */
function InsightCards({ stats }: { stats: StatItem[] }) {
  const insights: Array<{ icon: typeof AlertCircle; color: string; bg: string; text: string }> = []

  for (const s of stats) {
    // 高缺失率
    if (s.null_rate > 0.3) {
      insights.push({
        icon: AlertCircle,
        color: 'text-danger',
        bg: 'bg-danger-light',
        text: `"${s.field}" 缺失率高达 ${formatRate(s.null_rate)}，建议检查数据质量`,
      })
    }
    // 高度偏斜（std > mean * 2 说明分布很分散）
    if (s.mean && s.std && s.mean > 0 && s.std > s.mean * 2) {
      insights.push({
        icon: Activity,
        color: 'text-warning',
        bg: 'bg-warning-light',
        text: `"${s.field}" 标准差 (${formatNum(s.std)}) 远大于均值 (${formatNum(s.mean)})，数据分布高度分散`,
      })
    }
    // 极值跨度大（max > min * 100）
    if (s.min !== null && s.max !== null && s.min > 0 && s.max / s.min > 100) {
      insights.push({
        icon: ArrowUp,
        color: 'text-info',
        bg: 'bg-info-light',
        text: `"${s.field}" 极值跨度大 (${formatNum(s.min)} ~ ${formatNum(s.max)})，可能存在离群值`,
      })
    }
    // 大多数值集中（p50 === p75 === max 重尾分布）
    if (s.p50 !== null && s.max !== null && s.p50 !== 0 && s.max > Math.abs(s.p50) * 10) {
      insights.push({
        icon: Info,
        color: 'text-chart-6',
        bg: 'bg-[#F3F0FF]',
        text: `"${s.field}" 分布右偏 (P50=${formatNum(s.p50)}, Max=${formatNum(s.max)})，多数值集中在低区间`,
      })
    }
  }

  // 只保留前 4 条
  const top = insights.slice(0, 4)
  if (top.length === 0) return null

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {top.map((ins, i) => (
        <div
          key={i}
          className={`flex items-start gap-2 px-3 py-2.5 rounded-md text-[12px] ${ins.bg} ${ins.color}`}
        >
          <ins.icon className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span>{ins.text}</span>
        </div>
      ))}
    </div>
  )
}

function SummaryCards({ summary, loading }: { summary: SummaryResult | null; loading: boolean }) {
  if (loading) {
    return (
      <div className="grid grid-cols-4 gap-4">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="bg-white rounded-[10px] shadow-card p-4 flex items-center gap-3 animate-pulse">
            <div className="w-10 h-10 rounded-lg bg-muted" />
            <div className="flex-1 space-y-2">
              <div className="h-3 bg-muted rounded w-16" />
              <div className="h-5 bg-muted rounded w-24" />
            </div>
          </div>
        ))}
      </div>
    )
  }

  if (!summary) return null

  const timeRangeStr =
    summary.date_range?.min && summary.date_range?.max
      ? `${summary.date_range.min} ~ ${summary.date_range.max}`
      : '—'

  const cards = [
    {
      icon: Database,
      label: '总记录数',
      value: formatCount(summary.total_rows),
      color: 'text-primary',
      bg: 'bg-primary-light',
    },
    {
      icon: Hash,
      label: '字段总数',
      value: String(summary.total_columns),
      color: 'text-chart-2',
      bg: 'bg-success-light',
    },
    {
      icon: Key,
      label: '去重主键数',
      value: formatCount(summary.distinct_keys),
      color: 'text-chart-6',
      bg: 'bg-[#F3F0FF]',
    },
    {
      icon: Calendar,
      label: '数据时间范围',
      value: timeRangeStr,
      color: 'text-chart-3',
      bg: 'bg-warning-light',
    },
  ]

  return (
    <div className="grid grid-cols-4 gap-4">
      {cards.map((card) => (
        <div
          key={card.label}
          className="bg-white rounded-[10px] shadow-card p-4 flex items-center gap-3"
        >
          <div className={`w-10 h-10 rounded-lg ${card.bg} flex items-center justify-center`}>
            <card.icon className={`w-5 h-5 ${card.color}`} />
          </div>
          <div>
            <div className="text-[12px] text-muted-foreground">{card.label}</div>
            <div className="text-[15px] font-semibold text-foreground truncate max-w-[180px]">
              {card.value}
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

function InsightsPanel({ dsId, fields }: { dsId: string; fields: SchemaField[] }) {
  const [insights, setInsights] = useState<Array<{
    type: string; title: string; description: string; severity: string;
  }> | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleGenerate = async () => {
    const dimField = fields.find((f) => f.category === 'dimension')
    const measureField = fields.find((f) => f.category === 'measure')
    if (!dimField || !measureField) {
      setError('需要至少一个维度和一个度量字段才能生成洞察')
      return
    }
    setLoading(true)
    setError(null)
    setInsights(null)
    try {
      const res = await generateInsights({
        datasource_id: dsId,
        query_config: {
          dimensions: [dimField.name],
          measures: [{ field: measureField.name, agg: 'SUM' }],
        },
      })
      setInsights(res.insights)
    } catch (err) {
      setError(err instanceof Error ? err.message : '获取洞察失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-[12px] text-muted-foreground">AI 自动分析数据趋势、异常和机会，每次调用消耗 AI Token</p>

      <button
        onClick={handleGenerate}
        disabled={loading}
        className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50 transition-colors"
      >
        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
        {loading ? 'AI 分析中...' : insights ? '重新生成洞察' : '生成 AI 洞察'}
      </button>

      {error && (
        <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">{error}</div>
      )}

      {insights && insights.length === 0 && !loading && !error && (
        <div className="py-12 text-center space-y-3">
          <Sparkles className="w-10 h-10 text-muted-foreground/40 mx-auto" />
          <p className="text-[14px] font-medium text-foreground">暂无AI洞察</p>
          <p className="text-[12px] text-muted-foreground">AI 暂未生成有效的洞察分析，请稍后再试</p>
        </div>
      )}

      {insights && insights.length > 0 && (
        <div className="space-y-3">
          {insights.map((ins, i) => {
            const sevIcon = (s: string) => {
              if (s === 'warning') return AlertCircle
              if (s === 'success') return TrendingUp
              return Info
            }
            const sevStyle = (s: string) => {
              if (s === 'warning') return { color: '#ef4444', bg: '#fef2f2' }
              if (s === 'success') return { color: '#10b981', bg: '#ecfdf5' }
              return { color: '#3b82f6', bg: '#eff6ff' }
            }
            const typeLabel = (t: string) => {
              if (t === 'trend') return '趋势'
              if (t === 'anomaly') return '异常'
              if (t === 'opportunity') return '机会'
              return t
            }
            const ss = sevStyle(ins.severity)
            const Icon = sevIcon(ins.severity)
            return (
              <div key={i} className="bg-white rounded-[10px] shadow-card border border-border-light overflow-hidden">
                <div className="px-4 py-3 flex items-start gap-3">
                  <Icon className="w-4 h-4 mt-0.5 shrink-0" style={{ color: ss.color }} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[13px] font-semibold text-foreground">{ins.title}</span>
                      <span className="inline-flex px-2 py-0.5 rounded text-[11px] font-medium" style={{ backgroundColor: ss.bg, color: ss.color }}>
                        {typeLabel(ins.type)}
                      </span>
                    </div>
                    <p className="text-[12px] text-muted-foreground whitespace-pre-wrap">{ins.description}</p>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

type CleanIssueItem = {
  field: string
  issue_type: string
  count: number
  percentage: number
  sample: unknown[]
  suggestion: string
  severity: string
}

const CLEAN_CATEGORIES: Array<{
  key: string
  label: string
  desc: string
  icon: typeof AlertCircle
  color: string
  bg: string
  checkType: string
  actions: Array<{ action: string; label: string; desc: string }>
}> = [
  {
    key: 'missing',
    label: '缺失值检测',
    desc: '统计每列 null 数量和占比，建议填充或删除',
    icon: AlertCircle,
    color: '#f59e0b',
    bg: '#fffbeb',
    checkType: 'missing',
    actions: [
      { action: 'drop_null', label: '删除缺失行', desc: '删除所有含 null 的记录' },
      { action: 'fill_mean', label: '填充均值', desc: '用列均值填充（仅数值字段）' },
      { action: 'fill_median', label: '填充中位数', desc: '用中位数填充（仅数值字段）' },
      { action: 'fill_mode', label: '填充众数', desc: '用出现最多的值填充' },
      { action: 'fill_ffill', label: '前向填充', desc: '用上一条记录的值填充' },
    ],
  },
  {
    key: 'outlier',
    label: '异常值检测 (Z-Score)',
    desc: 'Z-Score > 3 判为异常，适合正态分布数据',
    icon: Activity,
    color: '#ef4444',
    bg: '#fef2f2',
    checkType: 'outlier',
    actions: [
      { action: 'drop_negative', label: '删除异常值', desc: '删除 Z-Score > 3 的异常记录' },
    ],
  },
  {
    key: 'outlier_iqr',
    label: '异常值检测 (IQR)',
    desc: '超出 Q1-1.5×IQR ~ Q3+1.5×IQR 判为异常，适合偏态数据',
    icon: Activity,
    color: '#ef4444',
    bg: '#fef2f2',
    checkType: 'outlier_iqr',
    actions: [
      { action: 'drop_negative', label: '删除异常值', desc: '删除 IQR 法标记的异常记录' },
    ],
  },
  {
    key: 'duplicate',
    label: '重复行检测',
    desc: '对所有列去重计数，展示重复数据供确认',
    icon: Trash2,
    color: '#8b5cf6',
    bg: '#f3f0ff',
    checkType: 'duplicate',
    actions: [
      { action: 'drop_null', label: '删除重复行', desc: '仅保留首次出现的记录' },
    ],
  },
  {
    key: 'format',
    label: '日期格式检测',
    desc: '检测不符合 YYYY-MM-DD 格式的日期字段',
    icon: Calendar,
    color: '#3b82f6',
    bg: '#eff6ff',
    checkType: 'format',
    actions: [
      { action: 'standardize_date', label: '标记不符合项', desc: '标记需要手动处理的日期' },
    ],
  },
  {
    key: 'type_mismatch',
    label: '类型不一致检测',
    desc: '对比 Schema 定义与实际值类型，发现不匹配',
    icon: Info,
    color: '#06b6d4',
    bg: '#ecfeff',
    checkType: 'type_mismatch',
    actions: [],
  },
]

function CleanPanel({ dsId }: { dsId: string }) {
  const [detected, setDetected] = useState<Record<string, CleanIssueItem[]>>({})
  const [checking, setChecking] = useState<Record<string, boolean>>({})
  const [appliedMap, setAppliedMap] = useState<Record<string, Set<string>>>({})
  const [processing, setProcessing] = useState<string | null>(null)
  const [error, setError] = useState<Record<string, string>>({})

  const handleCheck = async (catKey: string, checkType: string) => {
    setChecking((prev) => ({ ...prev, [catKey]: true }))
    setError((prev) => { const n = { ...prev }; delete n[catKey]; return n })
    try {
      const res = await aiCleanDatasource(dsId, checkType)
      const issues = (res.issues || []) as CleanIssueItem[]
      setDetected((prev) => ({ ...prev, [catKey]: issues }))
    } catch (err: any) {
      setError((prev) => ({ ...prev, [catKey]: err?.message || '检测失败' }))
    } finally {
      setChecking((prev) => ({ ...prev, [catKey]: false }))
    }
  }

  const handleAction = async (catKey: string, action: string) => {
    const catIssues = detected[catKey] || []
    if (!catIssues.length) return
    const procKey = `${catKey}_${action}`
    setProcessing(procKey)
    for (const issue of catIssues) {
      if (appliedMap[catKey]?.has(issue.field)) continue
      try {
        const payload = { datasource_id: dsId as any, rules: [{ field: issue.field, action }] }
        const result = await applyClean(payload as any)
        if (result.results?.[0]?.success) {
          setAppliedMap((prev) => {
            const s = new Set(prev[catKey] || [])
            s.add(issue.field)
            return { ...prev, [catKey]: s }
          })
        }
      } catch { /* skip */ }
    }
    setProcessing(null)
  }

  const catIssues = (key: string) => detected[key] || []
  const catApplied = (key: string) => appliedMap[key] || new Set()

  return (
    <div className="space-y-4">
      <p className="text-[12px] text-muted-foreground">选择一项检测，系统会分析数据并给出清洗建议</p>

      {CLEAN_CATEGORIES.map((cat) => {
        const issues = catIssues(cat.key)
        const hasDetected = issues.length > 0
        const allApplied = hasDetected && issues.every((i) => catApplied(cat.key).has(i.field))
        const isChecking = checking[cat.key]
        const errMsg = error[cat.key]
        const totalCount = hasDetected ? issues.reduce((s, i) => s + i.count, 0) : 0

        return (
          <div key={cat.key} className="bg-white rounded-[10px] shadow-card border border-border-light overflow-hidden">
            {/* 卡片头部 */}
            <div className="flex items-center gap-3 px-4 py-3">
              <div className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0" style={{ backgroundColor: cat.bg }}>
                <cat.icon className="w-4 h-4" style={{ color: cat.color }} />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-[13px] font-semibold text-foreground">{cat.label}</span>
                  {hasDetected && (
                    <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium" style={{ backgroundColor: cat.bg, color: cat.color }}>
                      {issues.length} 字段 · {totalCount} 条
                    </span>
                  )}
                  {allApplied && <CheckCircle2 className="w-3.5 h-3.5 text-success" />}
                </div>
                <p className="text-[11px] text-muted-foreground mt-0.5">{cat.desc}</p>
              </div>
              <button
                onClick={() => handleCheck(cat.key, cat.checkType)}
                disabled={isChecking}
                className={`shrink-0 inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[12px] font-medium transition-colors ${
                  isChecking ? 'bg-muted text-muted-foreground cursor-wait' : 'text-white hover:opacity-90'
                }`}
                style={!isChecking ? { backgroundColor: cat.color } : undefined}
              >
                {isChecking ? (
                  <><Loader2 className="w-3 h-3 animate-spin" />检测中</>
                ) : hasDetected ? '重新检测' : '开始检测'}
              </button>
            </div>

            {/* 检测结果 */}
            {hasDetected && (
              <div className="border-t border-border-light">
                {/* 字段明细 */}
                <div className="px-4 py-2 space-y-1.5 max-h-[260px] overflow-y-auto">
                  {issues.map((issue) => {
                    const done = catApplied(cat.key).has(issue.field)
                    const pct = typeof issue.percentage === 'number' ? issue.percentage : 0
                    return (
                      <div key={issue.field} className="flex items-start justify-between py-1.5 gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className={`text-[12px] font-medium ${done ? 'text-muted-foreground line-through' : 'text-foreground'}`}>
                              {issue.field}
                            </span>
                            <span className="text-[11px] text-muted-foreground">
                              {issue.count} 行 ({pct.toFixed(1)}%)
                            </span>
                            {done && <CheckCircle2 className="w-3 h-3 text-success shrink-0" />}
                          </div>
                          {issue.suggestion && (
                            <p className="text-[11px] text-muted-foreground mt-0.5">{issue.suggestion}</p>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>

                {/* 操作按钮 */}
                {!allApplied && cat.actions.length > 0 && (
                  <div className="px-4 py-2.5 bg-muted/20 border-t border-border-light">
                    <p className="text-[11px] text-muted-foreground mb-2">处理方式：</p>
                    <div className="flex flex-wrap gap-2">
                      {cat.actions.map((act) => {
                        const procKey = `${cat.key}_${act.action}`
                        const isProcessing = processing === procKey
                        return (
                          <button
                            key={act.action}
                            onClick={() => handleAction(cat.key, act.action)}
                            disabled={isProcessing}
                            title={act.desc}
                            className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[12px] font-medium transition-colors ${
                              isProcessing ? 'bg-ai/50 text-white cursor-wait' : 'text-white hover:opacity-90'
                            }`}
                            style={!isProcessing ? { backgroundColor: cat.color } : undefined}
                          >
                            {isProcessing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Wand2 className="w-3 h-3" />}
                            {isProcessing ? '处理中...' : act.label}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 错误提示 */}
            {errMsg && (
              <div className="px-4 py-2 border-t border-border-light text-[12px] text-danger">{errMsg}</div>
            )}
          </div>
        )
      })}
    </div>
  )
}

type TabKey = 'describe' | 'correlation' | 'insights' | 'clean'

export default function StatisticsPage() {
  const [datasources, setDatasources] = useState<DataSource[]>([])
  const [dsLoading, setDsLoading] = useState(true)
  const [selectedDsId, setSelectedDsId] = useState<string>('')
  const [activeTab, setActiveTab] = useState<TabKey>('describe')
  const [stats, setStats] = useState<StatItem[] | null>(null)
  const [corrData, setCorrData] = useState<{ fields: string[]; matrix: number[][] } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Summary
  const [summary, setSummary] = useState<SummaryResult | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)

  // Preview
  const [preview, setPreview] = useState<PreviewResult | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)

  // Fields from datasource
  const [fields, setFields] = useState<SchemaField[]>([])
  const [fieldsLoading, setFieldsLoading] = useState(false)

  useEffect(() => {
    listDatasources({ pageSize: 100 })
      .then((res) => setDatasources(res.items ?? []))
      .catch(() => setDatasources([]))
      .finally(() => setDsLoading(false))
  }, [])

  // Fetch fields and summary when datasource changes
  useEffect(() => {
    if (!selectedDsId) {
      setFields([])
      setSummary(null)
      return
    }
    setFieldsLoading(true)
    getDatasource(selectedDsId)
      .then((ds) => {
        setFields(ds.schemaMeta?.fields ?? [])
      })
      .catch(() => setFields([]))
      .finally(() => setFieldsLoading(false))

    setSummaryLoading(true)
    getSummary(selectedDsId)
      .then((data) => setSummary(data))
      .catch(() => setSummary(null))
      .finally(() => setSummaryLoading(false))

    setPreviewLoading(true)
    getPreview(selectedDsId, 5)
      .then((data) => setPreview(data))
      .catch(() => setPreview(null))
      .finally(() => setPreviewLoading(false))
  }, [selectedDsId])

  const [refreshing, setRefreshing] = useState(false)

  function handleRefresh() {
    if (!selectedDsId || refreshing) return
    setRefreshing(true)
    setStats(null); setCorrData(null); setSummary(null); setPreview(null)
    setError(null)

    // Re-fetch summary
    setSummaryLoading(true)
    getSummary(selectedDsId)
      .then(setSummary)
      .catch(() => {})
      .finally(() => setSummaryLoading(false))

    // Re-fetch preview
    setPreviewLoading(true)
    getPreview(selectedDsId, 5)
      .then(setPreview)
      .catch(() => {})
      .finally(() => setPreviewLoading(false))

    // Re-fetch current tab data
    if (activeTab === 'describe') {
      setLoading(true)
      describeStatistics(selectedDsId)
        .then((data) => { if (data?.statistics) setStats(data.statistics) })
        .catch((err) => setError(err instanceof Error ? err.message : '获取统计失败'))
        .finally(() => { setLoading(false); setRefreshing(false) })
    } else if (activeTab === 'correlation') {
      setLoading(true)
      correlationMatrix(selectedDsId)
        .then(setCorrData)
        .catch((err) => setError(err instanceof Error ? err.message : '获取相关性失败'))
        .finally(() => { setLoading(false); setRefreshing(false) })
    } else {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    if (!selectedDsId) {
      setStats(null)
      setCorrData(null)
      setError(null)
      return
    }
    if (activeTab !== 'describe' && activeTab !== 'correlation') {
      return
    }
    setLoading(true)
    setError(null)
    if (activeTab === 'describe') {
      describeStatistics(selectedDsId)
        .then((data) => {
          if (data?.statistics) setStats(data.statistics)
        })
        .catch((err) => {
          const msg = err instanceof Error ? err.message : '获取统计失败'
          setError(msg)
          setStats(null)
        })
        .finally(() => setLoading(false))
    } else {
      correlationMatrix(selectedDsId)
        .then((data) => setCorrData(data))
        .catch((err) => {
          const msg = err instanceof Error ? err.message : '获取相关性失败'
          setError(msg)
          setCorrData(null)
        })
        .finally(() => setLoading(false))
    }
  }, [selectedDsId, activeTab])

  const tabDefs: Array<{ key: TabKey; label: string; icon: typeof Gauge }> = [
    { key: 'describe', label: '描述性统计', icon: Gauge },
    { key: 'correlation', label: '相关性分析', icon: Activity },
    { key: 'insights', label: 'AI洞察', icon: Sparkles },
    { key: 'clean', label: '数据清洗', icon: Wand2 },
  ]

  return (
    <div className="flex-1 p-6 space-y-5">
      <header className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-primary/10 flex items-center justify-center">
          <Lightbulb className="w-5 h-5 text-primary" />
        </div>
        <div>
          <h1 className="text-[17px] font-semibold text-foreground">智能洞察</h1>
          <p className="text-[12px] text-muted-foreground mt-0.5">
            自动分析数据概览、字段统计、相关性分析和AI洞察
          </p>
        </div>
      </header>

      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <label className="text-[13px] text-muted-foreground">数据源</label>
          <select
            value={selectedDsId}
            onChange={(e) => setSelectedDsId(e.target.value)}
            className="px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer min-w-[220px]"
          >
            <option value="">
              {dsLoading ? '加载中...' : '选择数据源'}
            </option>
            {datasources.map((ds) => (
              <option key={ds.id} value={ds.id}>
                {ds.name}
              </option>
            ))}
          </select>
        </div>
        {selectedDsId && (
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="flex items-center gap-1.5 px-3 py-2 text-[12px] rounded-lg border border-border bg-white text-muted-foreground hover:text-foreground hover:border-primary/40 transition-colors disabled:opacity-50"
            title="刷新当前数据"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            刷新
          </button>
        )}
      </div>

      {selectedDsId ? (
        <SummaryCards summary={summary} loading={summaryLoading} />
      ) : null}

      {/* 数据预览 + 表结构 */}
      {selectedDsId && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* 数据预览 */}
          <div className="bg-white rounded-[10px] shadow-card p-4">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-[13px] font-semibold text-foreground">数据预览</h3>
              <button
                onClick={() => {
                  setPreviewLoading(true)
                  getPreview(selectedDsId, 5)
                    .then(setPreview)
                    .catch(() => {})
                    .finally(() => setPreviewLoading(false))
                }}
                disabled={previewLoading}
                className="text-[12px] text-primary hover:text-primary-hover flex items-center gap-1 transition-colors"
              >
                <RefreshCw className={`w-3 h-3 ${previewLoading ? 'animate-spin' : ''}`} />
                刷新
              </button>
            </div>
            {previewLoading ? (
              <div className="py-8 text-center text-muted-foreground text-[12px]">
                <Loader2 className="w-4 h-4 animate-spin mx-auto mb-2" />
                加载中...
              </div>
            ) : preview?.rows?.length ? (
              <div className="overflow-x-auto max-h-[260px] overflow-y-auto">
                <table className="w-full text-[12px] border border-border-light">
                  <thead className="sticky top-0 z-10">
                    <tr className="bg-muted">
                      <th className="px-2 py-1.5 text-left font-medium text-muted-foreground text-[11px] border-r border-border-light w-[40px]">#</th>
                      {preview.columns.map((col, i) => (
                        <th key={i} className="px-3 py-1.5 text-left font-medium text-muted-foreground text-[11px] border-r border-border-light whitespace-nowrap">
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((row, i) => (
                      <tr key={i} className="border-t border-border-light hover:bg-muted/50">
                        <td className="px-2 py-1.5 text-muted-foreground text-[11px] border-r border-border-light">{i + 1}</td>
                        {preview.columns.map((col, j) => (
                          <td key={j} className="px-3 py-1.5 text-card-foreground border-r border-border-light max-w-[180px] truncate" title={row[col] === null ? 'NULL' : String(row[col])}>
                            {row[col] === null ? <span className="text-muted-foreground italic">NULL</span> : String(row[col])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="py-8 text-center text-muted-foreground text-[12px]">
                暂无预览数据
              </div>
            )}
          </div>

          {/* 表结构概览 */}
          <div className="bg-white rounded-[10px] shadow-card p-4">
            <h3 className="text-[13px] font-semibold text-foreground mb-3">
              表结构 ({fields.length} 个字段)
            </h3>
            {fieldsLoading ? (
              <div className="py-8 text-center text-muted-foreground text-[12px]">
                <Loader2 className="w-4 h-4 animate-spin mx-auto mb-2" />
                加载中...
              </div>
            ) : fields.length > 0 ? (
              <div className="overflow-x-auto max-h-[260px] overflow-y-auto">
                <table className="w-full text-[12px] border border-border-light">
                  <thead className="sticky top-0 z-10">
                    <tr className="bg-muted">
                      <th className="px-3 py-1.5 text-left font-medium text-muted-foreground text-[11px] border-r border-border-light">字段名</th>
                      <th className="px-3 py-1.5 text-left font-medium text-muted-foreground text-[11px] border-r border-border-light">类型</th>
                      <th className="px-3 py-1.5 text-left font-medium text-muted-foreground text-[11px]">分类</th>
                    </tr>
                  </thead>
                  <tbody>
                    {fields.map((f) => {
                      const catLabel = f.category === 'measure' ? '度量' : f.category === 'dimension' ? '维度' : f.category === 'time' ? '时间' : '—'
                      const catColor = f.category === 'measure' ? 'text-chart-2' : f.category === 'dimension' ? 'text-primary' : f.category === 'time' ? 'text-chart-3' : 'text-muted-foreground'
                      return (
                        <tr key={f.name} className="border-t border-border-light hover:bg-muted/50">
                          <td className="px-3 py-1.5 font-medium text-foreground border-r border-border-light">{f.name}</td>
                          <td className="px-3 py-1.5 text-muted-foreground border-r border-border-light text-[11px] font-mono">{f.dataType || '—'}</td>
                          <td className={`px-3 py-1.5 text-[11px] font-medium ${catColor}`}>{catLabel}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="py-8 text-center text-muted-foreground text-[12px]">
                暂无字段信息
              </div>
            )}
          </div>
        </div>
      )}

      <div className="flex gap-0 border-b border-border-light">
        {tabDefs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-[13px] font-medium border-b-2 transition-colors flex items-center gap-1.5 ${
              activeTab === tab.key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
            <tab.icon className="w-3.5 h-3.5" />
            {tab.label}
          </button>
        ))}
      </div>

      <div>
        {!selectedDsId ? (
          <div className="py-16 text-center text-[13px] text-muted-foreground">
            <BarChart3 className="w-10 h-10 text-muted-foreground/40 mx-auto mb-3" />
            请先选择一个数据源
          </div>
        ) : activeTab === 'insights' ? (
          <InsightsPanel dsId={selectedDsId} fields={fields} />
        ) : activeTab === 'clean' ? (
          <CleanPanel dsId={selectedDsId} />
        ) : loading ? (
          <div className="py-16 text-center text-[13px] text-muted-foreground">
            <span className="inline-flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在加载数据...
            </span>
          </div>
        ) : error ? (
          <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">
            {error}
          </div>
        ) : activeTab === 'describe' ? (
          stats ? (
            <>
              <InsightCards stats={stats} />
              <DescribeTable stats={stats} />
            </>
          ) : null
        ) : corrData ? (
          corrData.fields.length > 1 ? (
            <div className="h-[500px]">
              <EChartsHeatmap
                xFields={corrData.fields}
                yFields={corrData.fields}
                matrix={corrData.matrix}
                title="字段相关性矩阵"
              />
            </div>
          ) : (
            <div className="py-10 text-center text-[13px] text-muted-foreground">
              该数据源没有足够的数值字段进行相关性分析
            </div>
          )
        ) : null}
      </div>
    </div>
  )
}
