import { useEffect, useState, useCallback, useMemo } from 'react'
import {
  ScrollText,
  Loader2,
  Search,
  ChevronLeft,
  ChevronRight,
  Filter,
  Activity,
  AlertTriangle,
  Database,
  Clock,
  Download,
} from 'lucide-react'
import {
  listOperationLogs,
  getAuditSummary,
  exportLogsCsv,
  RESOURCE_TYPE_LABELS,
  actionLabel,
  statusColor,
} from '../../api/audit'
import type { OperationLogItem, AuditSummary } from '../../api/audit'
import { useToast } from '../../components/ui/Toast'

const PAGE_SIZE = 25

const ALL_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
const COMMON_RESOURCES = [
  'auth',
  'user',
  'canvas',
  'datasource',
  'dashboard',
  'report',
  'ai',
  'insight',
  'notification',
  'permission',
  'audit',
]

const METHOD_COLORS: Record<string, string> = {
  GET: 'bg-info-light text-info',
  POST: 'bg-success-light text-success',
  PUT: 'bg-warning-light text-warning',
  PATCH: 'bg-warning-light text-warning',
  DELETE: 'bg-danger-light text-danger',
}

function formatTime(iso: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export default function AuditPage() {
  const toast = useToast()

  const [items, setItems] = useState<OperationLogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [summary, setSummary] = useState<AuditSummary | null>(null)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)

  // 筛选条件
  const [search, setSearch] = useState('')
  const [resourceType, setResourceType] = useState('')
  const [method, setMethod] = useState('')
  const [statusFilter, setStatusFilter] = useState<'' | 'success' | 'error'>('')

  const [refreshTick, setRefreshTick] = useState(0)
  const [exporting, setExporting] = useState(false)

  const handleExport = async () => {
    setExporting(true)
    try {
      await exportLogsCsv({
        search: search || undefined,
        resourceType: resourceType || undefined,
        method: method || undefined,
        statusCode: statusFilter === 'error' ? 400 : undefined,
      })
      toast.success('CSV 已开始下载')
    } catch (e: any) {
      toast.error(e?.response?.data?.detail?.message || e?.message || '导出失败')
    } finally {
      setExporting(false)
    }
  }

  const fetchList = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listOperationLogs({
        page,
        pageSize: PAGE_SIZE,
        search: search || undefined,
        resourceType: resourceType || undefined,
        method: method || undefined,
        statusCode:
          statusFilter === 'success'
            ? undefined
            : statusFilter === 'error'
              ? 400
              : undefined,
        // 简化：只过滤 ≥400
      })
      setItems(res.items)
      setTotal(res.total)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail?.message || '加载日志失败')
      setItems([])
      setTotal(0)
    } finally {
      setLoading(false)
    }
  }, [page, search, resourceType, method, statusFilter, toast])

  const fetchSummary = useCallback(async () => {
    try {
      const s = await getAuditSummary()
      setSummary(s)
    } catch {
      // 静默
    }
  }, [])

  useEffect(() => {
    fetchList()
  }, [fetchList, refreshTick])

  useEffect(() => {
    fetchSummary()
  }, [fetchSummary, refreshTick])

  useEffect(() => {
    setPage(1)
  }, [search, resourceType, method, statusFilter])

  // 客户端二次过滤：success/error，因为后端 statusCode 简化只发 400 阈值
  const filteredItems = useMemo(() => {
    if (statusFilter === 'success') {
      return items.filter((i) => i.statusCode < 400)
    }
    if (statusFilter === 'error') {
      return items.filter((i) => i.statusCode >= 400)
    }
    return items
  }, [items, statusFilter])

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="flex-1 p-6 space-y-5">
      {/* 顶部 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-md bg-primary-light flex items-center justify-center">
            <ScrollText className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h1 className="text-[17px] font-semibold text-foreground">日志审计</h1>
            <p className="text-[12px] text-muted-foreground mt-0.5">
              平台操作轨迹 · 共 {total} 条记录
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleExport}
            disabled={exporting || total === 0}
            className="px-3 py-1.5 text-[12px] border border-border rounded-md hover:bg-muted/50 text-muted-foreground disabled:opacity-50 flex items-center gap-1.5"
          >
            {exporting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
            导出 CSV
          </button>
          <button
            onClick={() => setRefreshTick((t) => t + 1)}
            className="px-3 py-1.5 text-[12px] border border-border rounded-md hover:bg-muted/50 text-muted-foreground"
          >
            刷新
          </button>
        </div>
      </div>

      {/* 24h 概览 */}
      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <SummaryCard
            icon={Activity}
            label="近 24h 操作"
            value={summary.total24h}
            tone="primary"
          />
          <SummaryCard
            icon={AlertTriangle}
            label="近 24h 错误"
            value={summary.error24h}
            tone={summary.error24h > 0 ? 'danger' : 'muted'}
          />
          <div className="col-span-2 bg-white rounded-md shadow-card border border-border p-3">
            <div className="flex items-center gap-2 mb-2 text-[12px] text-muted-foreground">
              <Database className="w-3.5 h-3.5" />
              资源分布
            </div>
            <div className="flex flex-wrap gap-2">
              {summary.byResource.slice(0, 6).map((r) => (
                <span
                  key={r.resourceType}
                  className="text-[11px] px-2 py-1 rounded bg-muted text-foreground"
                >
                  {RESOURCE_TYPE_LABELS[r.resourceType] ?? r.resourceType}{' '}
                  <span className="text-muted-foreground">· {r.count}</span>
                </span>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* 筛选 */}
      <div className="bg-white rounded-md shadow-card border border-border p-4 flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索路径或动作"
            className="w-full pl-9 pr-3 py-2 border border-border rounded-md bg-input text-sm"
          />
        </div>
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Filter className="w-3.5 h-3.5" />
          筛选
        </div>
        <select
          value={resourceType}
          onChange={(e) => setResourceType(e.target.value)}
          className="px-3 py-2 border border-border rounded-md bg-input text-sm"
        >
          <option value="">全部资源</option>
          {COMMON_RESOURCES.map((r) => (
            <option key={r} value={r}>
              {RESOURCE_TYPE_LABELS[r] ?? r}
            </option>
          ))}
        </select>
        <select
          value={method}
          onChange={(e) => setMethod(e.target.value)}
          className="px-3 py-2 border border-border rounded-md bg-input text-sm"
        >
          <option value="">全部方法</option>
          {ALL_METHODS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as '' | 'success' | 'error')}
          className="px-3 py-2 border border-border rounded-md bg-input text-sm"
        >
          <option value="">全部状态</option>
          <option value="success">成功 (2xx/3xx)</option>
          <option value="error">失败 (4xx/5xx)</option>
        </select>
      </div>

      {/* 表格 */}
      <div className="bg-white rounded-md shadow-card border border-border overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16 text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin mr-2" />
            加载中...
          </div>
        ) : filteredItems.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <ScrollText className="w-10 h-10 mb-2 opacity-40" />
            <p className="text-[13px]">没有匹配的日志</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[12.5px]">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th className="text-left font-medium px-3 py-2.5 whitespace-nowrap">时间</th>
                  <th className="text-left font-medium px-3 py-2.5">操作者</th>
                  <th className="text-left font-medium px-3 py-2.5">动作</th>
                  <th className="text-left font-medium px-3 py-2.5">资源</th>
                  <th className="text-left font-medium px-3 py-2.5">方法</th>
                  <th className="text-left font-medium px-3 py-2.5">路径</th>
                  <th className="text-center font-medium px-3 py-2.5">状态</th>
                  <th className="text-right font-medium px-3 py-2.5">耗时</th>
                  <th className="text-left font-medium px-3 py-2.5">IP</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((it) => (
                  <tr
                    key={it.id}
                    className="border-t border-border-light hover:bg-muted/30 transition-colors"
                  >
                    <td className="px-3 py-2 text-muted-foreground font-mono text-[11.5px] whitespace-nowrap">
                      {formatTime(it.createdAt)}
                    </td>
                    <td className="px-3 py-2">
                      {it.userId ? (
                        <div>
                          <div className="text-foreground">{it.userDisplayName || '—'}</div>
                          <div className="text-[10.5px] text-muted-foreground font-mono">
                            {it.userEmail}
                          </div>
                        </div>
                      ) : (
                        <span className="text-muted-foreground italic">匿名</span>
                      )}
                    </td>
                    <td className="px-3 py-2">{actionLabel(it.action)}</td>
                    <td className="px-3 py-2">
                      <span className="text-[11px] px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                        {RESOURCE_TYPE_LABELS[it.resourceType] ?? it.resourceType}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className={`text-[10.5px] px-1.5 py-0.5 rounded font-mono font-medium ${METHOD_COLORS[it.method] ?? 'bg-muted text-foreground'}`}
                      >
                        {it.method}
                      </span>
                    </td>
                    <td
                      className="px-3 py-2 font-mono text-[11px] text-muted-foreground max-w-[280px] truncate"
                      title={it.path}
                    >
                      {it.path}
                    </td>
                    <td className="px-3 py-2 text-center">
                      <span
                        className={`inline-block min-w-[36px] text-[11px] px-1.5 py-0.5 rounded font-mono font-medium ${statusColor(it.statusCode)}`}
                      >
                        {it.statusCode}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {formatDuration(it.durationMs)}
                    </td>
                    <td className="px-3 py-2 text-muted-foreground font-mono text-[11px]">
                      {it.ipAddress || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {total > 0 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-border-light text-[12px] text-muted-foreground">
            <div className="flex items-center gap-2">
              <Clock className="w-3.5 h-3.5" />
              共 {total} 条 · 第 {page} / {totalPages} 页
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="p-1.5 border border-border rounded-md hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="p-1.5 border border-border rounded-md hover:bg-muted/50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function SummaryCard({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: typeof Activity
  label: string
  value: number
  tone: 'primary' | 'danger' | 'muted'
}) {
  const toneClass =
    tone === 'primary'
      ? 'bg-primary-light text-primary'
      : tone === 'danger'
        ? 'bg-danger-light text-danger'
        : 'bg-muted text-muted-foreground'
  return (
    <div className="bg-white rounded-md shadow-card border border-border p-3 flex items-center gap-3">
      <div className={`w-10 h-10 rounded-md flex items-center justify-center ${toneClass}`}>
        <Icon className="w-5 h-5" />
      </div>
      <div>
        <div className="text-[11px] text-muted-foreground">{label}</div>
        <div className="text-[20px] font-semibold text-foreground tabular-nums">{value}</div>
      </div>
    </div>
  )
}
