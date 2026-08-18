import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { getSharedView } from '../../api/public'

interface SharedData {
  type: 'dashboard' | 'report'
  title: string
  charts?: Array<{ chart_type: string; query_config: Record<string, unknown> }>
  blocks?: Array<Record<string, unknown>>
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
                {/* Render chart type label; actual data rendering is read-only */}
                <div className="h-64 bg-muted rounded flex items-center justify-center text-muted-foreground">
                  图表 #{i + 1} ({chart.chart_type})
                </div>
              </div>
            ))}
          </div>
        )}
        {data.type === 'report' && data.blocks && (
          <div className="space-y-4">
            {data.blocks.map((block: any, i) => (
              <div key={i} className="bg-card rounded-lg border border-border p-4">
                {block.type === 'text' && <p>{block.content?.text}</p>}
                {block.type === 'title' && <h2 className="text-lg font-semibold">{block.content?.text}</h2>}
                {block.type === 'chart' && (
                  <div className="h-64 bg-muted rounded flex items-center justify-center text-muted-foreground">
                    图表
                  </div>
                )}
                {block.type === 'divider' && <hr />}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
