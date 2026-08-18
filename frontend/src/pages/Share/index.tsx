import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { getSharedView } from '../../api/public'
import type { CanvasBlock, ChartQueryConfig, QueryResult } from '../../api/types'
import ChartRenderer from '../../components/charts/ChartRenderer'

interface SharedData {
  type: 'dashboard' | 'report'
  title: string
  charts?: Array<{ chart_type: string; query_config: Record<string, unknown> }>
  blocks?: { blocks: CanvasBlock[] }
}

const LoadingSpinner = () => (
  <div className="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full" />
)

export default function SharePage() {
  const { token } = useParams<{ token: string }>()
  const [data, setData] = useState<SharedData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  useEffect(() => {
    if (!token) { setError(true); setLoading(false); return }
    getSharedView(token)
      .then(setData)
      .catch(() => setError(true))
      .finally(() => setLoading(false))
  }, [token])

  if (loading) return <div className="flex items-center justify-center h-screen"><LoadingSpinner /></div>
  if (error || !data) return (
    <div className="flex flex-col items-center justify-center h-screen gap-4">
      <div className="text-6xl">📊</div>
      <h1 className="text-2xl font-semibold">分享链接不存在或已失效</h1>
      <p className="text-muted-foreground">请联系分享者重新获取链接</p>
    </div>
  )

  // 从嵌套结构中提取 blocks 数组
  const blocks: CanvasBlock[] = data.type === 'report' && data.blocks
    ? (Array.isArray(data.blocks) ? data.blocks : data.blocks.blocks ?? [])
    : []

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4">
        <h1 className="text-xl font-semibold">{data.title}</h1>
        <p className="text-sm text-muted-foreground">Lvco BI · 公开分享</p>
      </header>
      <main className="max-w-6xl mx-auto p-6">
        {data.type === 'dashboard' && data.charts && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {data.charts.map((chart, i) => (
              <div key={i} className="bg-card rounded-lg border border-border p-4">
                <div className="text-sm text-muted-foreground mb-2">{chart.chart_type}</div>
                <div className="h-64 bg-muted rounded flex items-center justify-center text-muted-foreground">
                  图表 #{i + 1} ({chart.chart_type})
                </div>
              </div>
            ))}
          </div>
        )}
        {data.type === 'report' && (
          <div className="max-w-[900px] mx-auto space-y-5">
            {blocks.map((block: any, i) => {
              if (block.type === 'h1') {
                return (
                  <h1 key={i} className="text-[24px] font-bold text-foreground border-b-2 border-primary pb-2">
                    {String(block.content ?? '')}
                  </h1>
                )
              }
              if (block.type === 'h2') {
                return (
                  <h2 key={i} className="text-[19px] font-semibold text-foreground mt-6">
                    {String(block.content ?? '')}
                  </h2>
                )
              }
              if (block.type === 'text') {
                return (
                  <p key={i} className="text-[14px] leading-relaxed text-card-foreground whitespace-pre-wrap">
                    {String(block.content ?? '')}
                  </p>
                )
              }
              if (block.type === 'image') {
                const src = block.src as string
                if (!src) return null
                return (
                  <div key={i} className="border rounded-lg overflow-hidden bg-muted/30">
                    <img src={src} alt={(block.alt as string) || ''} className="max-w-full max-h-[500px] object-contain mx-auto" />
                  </div>
                )
              }
              if (block.type === 'chart') {
                const config = block._chartConfig as ChartQueryConfig | undefined
                const result = block._chartResult as QueryResult | undefined
                const renderer = (block.renderer as "recharts" | "echarts" | undefined) || "echarts"
                const palette = (block.palette as string) || undefined
                return (
                  <div key={i} className="bg-white rounded-[10px] border border-border-light p-5 shadow-sm">
                    <div className="flex items-center gap-2 mb-3">
                      <span className="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium bg-muted text-muted-foreground">
                        图表
                      </span>
                      <span className="text-[13px] font-semibold text-foreground">
                        {(block.title as string) || '图表'}
                      </span>
                    </div>
                    <div style={{ height: 360 }}>
                      {config && result ? (
                        <ChartRenderer config={config} result={result} renderer={renderer} palette={palette} />
                      ) : (
                        <div className="flex items-center justify-center h-full text-[12px] text-muted-foreground">
                          图表数据不可用
                        </div>
                      )}
                    </div>
                  </div>
                )
              }
              return null
            })}
          </div>
        )}
      </main>
    </div>
  )
}
