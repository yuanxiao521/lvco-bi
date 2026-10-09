import apiClient from './client'

const STATS_TIMEOUT = 120000 // 统计分析查询可能较慢，给 2 分钟

export async function describeStatistics(datasourceId: string) {
  const res = await apiClient.post('/statistics/describe', { datasource_id: datasourceId }, { timeout: STATS_TIMEOUT })
  return res.data.data as {
    fields: string[]
    statistics: Array<{
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
    }>
  }
}

export async function correlationMatrix(datasourceId: string, fields?: string[]) {
  const res = await apiClient.post('/statistics/correlation', { datasource_id: datasourceId, fields }, { timeout: STATS_TIMEOUT })
  return res.data.data as { fields: string[]; matrix: number[][] }
}

export interface RankingParams {
  dimension: string
  metric: { field: string; agg: string }
  limit?: number
  order?: 'asc' | 'desc'
}

export interface RankingResult {
  data: Array<{ label: string; value: number }>
}

export async function getRanking(datasourceId: string, params: RankingParams) {
  const res = await apiClient.post('/statistics/ranking', { datasource_id: datasourceId, ...params }, { timeout: STATS_TIMEOUT })
  return res.data.data as RankingResult
}

export interface ComparisonParams {
  date_field: string
  metric_field: string
  metric_agg: string
  period: 'month' | 'quarter' | 'year'
  compare_type: 'mom' | 'yoy'
  dimension?: string
}

export interface ComparisonResult {
  data: Array<{
    period: string
    value: number
    prev_value: number
    change_pct: number | null
  }>
}

export async function getComparison(datasourceId: string, params: ComparisonParams) {
  const res = await apiClient.post('/statistics/comparison', { datasource_id: datasourceId, ...params }, { timeout: STATS_TIMEOUT })
  return res.data.data as ComparisonResult
}

export interface SummaryResult {
  total_rows: number
  total_columns: number
  distinct_keys: number
  date_range: { min: string | null; max: string | null } | null
}

export async function getSummary(datasourceId: string) {
  const res = await apiClient.post('/statistics/summary', { datasource_id: datasourceId }, { timeout: STATS_TIMEOUT })
  return res.data.data as SummaryResult
}

export interface PreviewResult {
  columns: string[]
  rows: Array<Record<string, unknown>>
  total_previewed: number
}

export async function getPreview(datasourceId: string, limit = 5) {
  const res = await apiClient.post('/statistics/preview', { datasource_id: datasourceId, limit }, { timeout: STATS_TIMEOUT })
  return res.data.data as PreviewResult
}
