import { useEffect, useState } from 'react'
import {
  BarChart3,
  Loader2,
  Database,
  Hash,
  Key,
  Calendar,
  TrendingUp,
  TrendingDown,
  Minus,
} from 'lucide-react'
import ReactECharts from 'echarts-for-react'
import {
  describeStatistics,
  correlationMatrix,
  getRanking,
  getComparison,
  getSummary,
} from '../../api/statistics'
import type { RankingResult, ComparisonResult, SummaryResult } from '../../api/statistics'
import { listDatasources, getDatasource } from '../../api/datasources'
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

const AGG_OPTIONS = [
  { value: 'SUM', label: 'SUM' },
  { value: 'AVG', label: 'AVG' },
  { value: 'COUNT', label: 'COUNT' },
  { value: 'MAX', label: 'MAX' },
  { value: 'MIN', label: 'MIN' },
  { value: 'STDDEV', label: '标准差' },
  { value: 'MEDIAN', label: '中位数' },
  { value: 'COUNT_DISTINCT', label: '去重计数' },
]

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

function RankingPanel({
  fields,
  dsId,
}: {
  fields: SchemaField[]
  dsId: string
}) {
  const [dimension, setDimension] = useState('')
  const [metric, setMetric] = useState('')
  const [agg, setAgg] = useState('SUM')
  const [limit, setLimit] = useState(10)
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const [result, setResult] = useState<RankingResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const dimensionFields = fields.filter((f) => f.category === 'dimension' || f.category === 'key')
  const measureFields = fields.filter((f) => f.category === 'measure')

  const handleQuery = async () => {
    if (!dimension || !metric) {
      setError('请选择维度和度量字段')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const data = await getRanking(dsId, { dimension, metric: { field: metric, agg }, limit, order })
      setResult(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : '获取排名数据失败')
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const chartOption = result?.data?.length
    ? {
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: 120, right: 40, top: 10, bottom: 30 },
        xAxis: { type: 'value' },
        yAxis: {
          type: 'category',
          data: result.data.map((d) => d.label).reverse(),
          axisLabel: { width: 100, overflow: 'truncate' },
        },
        series: [
          {
            type: 'bar',
            data: result.data.map((d) => d.value).reverse(),
            itemStyle: { color: '#6366F1', borderRadius: [0, 4, 4, 0] },
          },
        ],
      }
    : null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 p-4 bg-white rounded-[10px] shadow-card">
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">维度字段</label>
          <select
            value={dimension}
            onChange={(e) => setDimension(e.target.value)}
            className="px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer min-w-[160px]"
          >
            <option value="">选择维度</option>
            {dimensionFields.map((f) => (
              <option key={f.name} value={f.name}>
                {f.displayName || f.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">度量字段</label>
          <select
            value={metric}
            onChange={(e) => setMetric(e.target.value)}
            className="px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer min-w-[160px]"
          >
            <option value="">选择度量</option>
            {measureFields.map((f) => (
              <option key={f.name} value={f.name}>
                {f.displayName || f.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">聚合方式</label>
          <select
            value={agg}
            onChange={(e) => setAgg(e.target.value)}
            className="px-2 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
          >
            {AGG_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">数量</label>
          <input
            type="number"
            value={limit}
            min={1}
            max={100}
            onChange={(e) => setLimit(Number(e.target.value) || 10)}
            className="px-2 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring w-[80px]"
          />
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">排序</label>
          <select
            value={order}
            onChange={(e) => setOrder(e.target.value as 'asc' | 'desc')}
            className="px-2 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
          >
            <option value="desc">降序</option>
            <option value="asc">升序</option>
          </select>
        </div>
        <button
          onClick={handleQuery}
          disabled={loading}
          className="px-5 py-2 rounded-md text-[13px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50 h-[37px]"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin mx-auto" /> : '查询'}
        </button>
      </div>

      {error ? (
        <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">{error}</div>
      ) : null}

      {loading ? (
        <div className="py-16 text-center text-[13px] text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
          正在加载排名数据...
        </div>
      ) : result?.data?.length ? (
        <div className="space-y-4">
          <div className="bg-white rounded-[10px] shadow-card p-4">
            <ReactECharts option={chartOption} style={{ height: Math.max(result.data.length * 30 + 60, 200) }} />
          </div>
          <div className="bg-white rounded-[10px] shadow-card overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="bg-muted">
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground text-[12px]">排名</th>
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground text-[12px]">{dimension}</th>
                  <th className="text-right px-4 py-2 font-medium text-muted-foreground text-[12px]">
                    {agg}({metric})
                  </th>
                </tr>
              </thead>
              <tbody>
                {result.data.map((d, i) => (
                  <tr key={i} className="border-t border-border-light">
                    <td className="px-4 py-2 text-card-foreground">{i + 1}</td>
                    <td className="px-4 py-2 font-medium text-foreground">{d.label}</td>
                    <td className="px-4 py-2 text-card-foreground text-right">{formatNum(d.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : result ? (
        <div className="py-10 text-center text-[13px] text-muted-foreground bg-white rounded-[10px] shadow-card">
          无排名数据
        </div>
      ) : null}
    </div>
  )
}

function ComparisonPanel({
  fields,
  dsId,
}: {
  fields: SchemaField[]
  dsId: string
}) {
  const [dateField, setDateField] = useState('')
  const [metric, setMetric] = useState('')
  const [agg, setAgg] = useState('SUM')
  const [period, setPeriod] = useState<'month' | 'quarter' | 'year'>('month')
  const [compareType, setCompareType] = useState<'mom' | 'yoy'>('mom')
  const [dimension, setDimension] = useState('')
  const [result, setResult] = useState<ComparisonResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const dateFields = fields.filter(
    (f) => f.category === 'time' || f.dataType === 'DATE' || f.dataType === 'TIMESTAMP' || f.dataType === 'DATETIME'
  )
  const measureFields = fields.filter((f) => f.category === 'measure')
  const dimensionFields = fields.filter((f) => f.category === 'dimension' || f.category === 'key')

  const handleQuery = async () => {
    if (!dateField || !metric) {
      setError('请选择日期字段和度量字段')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const data = await getComparison(dsId, {
        date_field: dateField,
        metric_field: metric,
        metric_agg: agg,
        period,
        compare_type: compareType,
        dimension: dimension || undefined,
      })
      setResult(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : '获取对比数据失败')
      setResult(null)
    } finally {
      setLoading(false)
    }
  }

  const chartOption = result?.data?.length
    ? {
        tooltip: { trigger: 'axis' },
        legend: { data: ['当前值', '上期值'], top: 0 },
        grid: { left: 60, right: 30, top: 40, bottom: 40 },
        xAxis: {
          type: 'category',
          data: result.data.map((d) => d.period),
          axisLabel: { rotate: 30 },
        },
        yAxis: { type: 'value' },
        series: [
          {
            name: '当前值',
            type: 'line',
            data: result.data.map((d) => d.value),
            smooth: true,
            itemStyle: { color: '#6366F1' },
          },
          {
            name: '上期值',
            type: 'line',
            data: result.data.map((d) => d.prev_value),
            smooth: true,
            itemStyle: { color: '#F59E0B' },
            lineStyle: { type: 'dashed' },
          },
        ],
      }
    : null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3 p-4 bg-white rounded-[10px] shadow-card">
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">日期字段</label>
          <select
            value={dateField}
            onChange={(e) => setDateField(e.target.value)}
            className="px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer min-w-[160px]"
          >
            <option value="">选择日期字段</option>
            {dateFields.map((f) => (
              <option key={f.name} value={f.name}>
                {f.displayName || f.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">度量字段</label>
          <select
            value={metric}
            onChange={(e) => setMetric(e.target.value)}
            className="px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer min-w-[160px]"
          >
            <option value="">选择度量</option>
            {measureFields.map((f) => (
              <option key={f.name} value={f.name}>
                {f.displayName || f.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">聚合方式</label>
          <select
            value={agg}
            onChange={(e) => setAgg(e.target.value)}
            className="px-2 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
          >
            {AGG_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">周期</label>
          <select
            value={period}
            onChange={(e) => setPeriod(e.target.value as 'month' | 'quarter' | 'year')}
            className="px-2 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
          >
            <option value="month">按月</option>
            <option value="quarter">按季度</option>
            <option value="year">按年</option>
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">对比类型</label>
          <select
            value={compareType}
            onChange={(e) => setCompareType(e.target.value as 'mom' | 'yoy')}
            className="px-2 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
          >
            <option value="mom">环比</option>
            <option value="yoy">同比</option>
          </select>
        </div>
        <div>
          <label className="block text-[12px] text-muted-foreground mb-1">
            维度 <span className="text-[10px]">(可选)</span>
          </label>
          <select
            value={dimension}
            onChange={(e) => setDimension(e.target.value)}
            className="px-3 py-2 text-[13px] rounded-md border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer min-w-[140px]"
          >
            <option value="">无</option>
            {dimensionFields.map((f) => (
              <option key={f.name} value={f.name}>
                {f.displayName || f.name}
              </option>
            ))}
          </select>
        </div>
        <button
          onClick={handleQuery}
          disabled={loading}
          className="px-5 py-2 rounded-md text-[13px] font-medium text-white bg-primary hover:bg-primary-hover disabled:opacity-50 h-[37px]"
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin mx-auto" /> : '查询'}
        </button>
      </div>

      {error ? (
        <div className="px-4 py-3 rounded-md bg-danger-light text-danger text-[13px]">{error}</div>
      ) : null}

      {loading ? (
        <div className="py-16 text-center text-[13px] text-muted-foreground">
          <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
          正在加载对比数据...
        </div>
      ) : result?.data?.length ? (
        <div className="space-y-4">
          <div className="bg-white rounded-[10px] shadow-card p-4">
            <ReactECharts option={chartOption} style={{ height: 350 }} />
          </div>
          <div className="bg-white rounded-[10px] shadow-card overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="bg-muted">
                  <th className="text-left px-4 py-2 font-medium text-muted-foreground text-[12px]">周期</th>
                  <th className="text-right px-4 py-2 font-medium text-muted-foreground text-[12px]">当前值</th>
                  <th className="text-right px-4 py-2 font-medium text-muted-foreground text-[12px]">上期值</th>
                  <th className="text-right px-4 py-2 font-medium text-muted-foreground text-[12px]">变化率</th>
                </tr>
              </thead>
              <tbody>
                {result.data.map((d, i) => {
                  const isPositive = d.change_pct !== null && d.change_pct > 0
                  const isNegative = d.change_pct !== null && d.change_pct < 0
                  return (
                    <tr key={i} className="border-t border-border-light">
                      <td className="px-4 py-2 font-medium text-foreground">{d.period}</td>
                      <td className="px-4 py-2 text-card-foreground text-right">{formatNum(d.value)}</td>
                      <td className="px-4 py-2 text-card-foreground text-right">{formatNum(d.prev_value)}</td>
                      <td className="px-4 py-2 text-right">
                        {d.change_pct !== null ? (
                          <span
                            className={`inline-flex items-center gap-1 font-medium ${
                              isPositive ? 'text-success' : isNegative ? 'text-danger' : 'text-muted-foreground'
                            }`}
                          >
                            {isPositive ? <TrendingUp className="w-3.5 h-3.5" /> : isNegative ? <TrendingDown className="w-3.5 h-3.5" /> : <Minus className="w-3.5 h-3.5" />}
                            {(d.change_pct > 0 ? '+' : '') + d.change_pct.toFixed(2)}%
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : result ? (
        <div className="py-10 text-center text-[13px] text-muted-foreground bg-white rounded-[10px] shadow-card">
          无对比数据
        </div>
      ) : null}
    </div>
  )
}

type TabKey = 'describe' | 'correlation' | 'comparison' | 'ranking'

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
  }, [selectedDsId])

  useEffect(() => {
    if (!selectedDsId) {
      setStats(null)
      setCorrData(null)
      setError(null)
      return
    }
    if (activeTab === 'comparison' || activeTab === 'ranking') {
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

  const tabDefs: Array<{ key: TabKey; label: string }> = [
    { key: 'describe', label: '描述性统计' },
    { key: 'correlation', label: '相关性分析' },
    { key: 'comparison', label: '对比分析' },
    { key: 'ranking', label: '排名分析' },
  ]

  return (
    <div className="flex-1 p-6 space-y-5">
      <header className="flex items-center justify-between">
        <h1 className="text-[17px] font-semibold text-foreground">统计分析</h1>
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
      </div>

      {selectedDsId ? (
        <SummaryCards summary={summary} loading={summaryLoading} />
      ) : null}

      <div className="flex gap-0 border-b border-border-light">
        {tabDefs.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-[13px] font-medium border-b-2 transition-colors ${
              activeTab === tab.key
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground'
            }`}
          >
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
        ) : activeTab === 'comparison' ? (
          fieldsLoading ? (
            <div className="py-16 text-center text-[13px] text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
              正在加载字段...
            </div>
          ) : (
            <ComparisonPanel fields={fields} dsId={selectedDsId} />
          )
        ) : activeTab === 'ranking' ? (
          fieldsLoading ? (
            <div className="py-16 text-center text-[13px] text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
              正在加载字段...
            </div>
          ) : (
            <RankingPanel fields={fields} dsId={selectedDsId} />
          )
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
            <DescribeTable stats={stats} />
          ) : null
        ) : corrData ? (
          corrData.fields.length > 1 ? (
            <EChartsHeatmap
              fields={corrData.fields}
              matrix={corrData.matrix}
              title="字段相关性矩阵"
            />
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
