export async function getSharedView(token: string) {
  // This is a public endpoint, so we need to call without auth token
  const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1'
  const res = await fetch(`${baseUrl}/public/share/${token}`)
  if (!res.ok) throw new Error('NOT_FOUND')
  const data = await res.json()
  return data.data as {
    type: 'dashboard' | 'report'
    title: string
    charts?: Array<{ chart_type: string; query_config: Record<string, unknown> }>
    blocks?: Array<Record<string, unknown>>
  }
}
