import apiClient, { unwrapApi } from "./client";

// ============ Types ============
export interface QueryConfigMeasure {
  field: string;
  agg: "SUM" | "AVG" | "MAX" | "MIN" | "COUNT" | "COUNT_DISTINCT";
}

export interface QueryConfigFilter {
  field: string;
  op: string;
  value: unknown;
}

export interface QueryConfig {
  table: string;
  timeField: string;
  measures: QueryConfigMeasure[];
  dimensions: string[];
  filters: QueryConfigFilter[];
  timeRangeDays: number;
}

export interface InsightRule {
  id: string;
  userId: string;
  datasourceId: string;
  datasourceName?: string;
  name: string;
  description: string | null;
  queryConfig: QueryConfig;
  detectTypes: string[];
  threshold: Record<string, unknown> | null;
  reportType: string;
  schedule: string;
  scheduleTime: string;
  enabled: boolean;
  autoCreated: boolean;
  lastRunAt: string | null;
  lastRunStatus: string | null;
  nextRunAt: string | null;
  createdAt: string;
  updatedAt: string | null;
}

export interface InsightRuleListResult {
  items: InsightRule[];
  total: number;
  page: number;
  pageSize: number;
}

export interface InsightSuggestion {
  id: string;
  datasourceId: string;
  tableName: string;
  timeField: string | null;
  measureFields: string[];
  dimensionFields: string[];
  suggestedName: string | null;
  suggestedConfig: QueryConfig | null;
  rationale: string | null;
  confidence: number | null;
  rowCountEstimate: number | null;
  updateFrequency: string | null;
  status: string;
  createdAt: string;
}

export interface InsightSuggestionListResult {
  items: InsightSuggestion[];
  total: number;
}

export interface DiscoverResult {
  suggestionsCreated: number;
  suggestions: InsightSuggestion[];
}

export interface RunRuleResult {
  recordId: string;
  status: string;
}

export interface InsightChart {
  chartType: string;
  title: string;
  config: Record<string, unknown>;
  data: Array<Record<string, unknown>>;
}

export interface InsightRecord {
  id: string;
  ruleId: string;
  ruleName: string;
  datasourceId: string;
  runAt: string;
  periodStart: string;
  periodEnd: string;
  status: string;
  hasAnomalies?: boolean;
  anomalyCount?: number;
  reportId?: string | null;
  errorMessage?: string | null;
  aiNarrative?: string | null;
  charts?: InsightChart[];
  rawData?: Array<Record<string, unknown>>;
  detectedAnomalies?: Array<Record<string, unknown>>;
  llmTokensInput?: number | null;
  llmTokensOutput?: number | null;
}

export interface InsightRecordListResult {
  items: InsightRecord[];
  total: number;
  page: number;
  pageSize: number;
}

// ============ Rule CRUD ============
export async function listInsightRules(params: {
  enabled?: boolean;
  page?: number;
  pageSize?: number;
}): Promise<InsightRuleListResult> {
  const response = await apiClient.get("/insights/rules", { params });
  return unwrapApi<InsightRuleListResult>(response.data);
}

export async function createInsightRule(body: CreateInsightRuleBody): Promise<InsightRule> {
  const response = await apiClient.post("/insights/rules", body);
  return unwrapApi<InsightRule>(response.data);
}

export type CreateInsightRuleBody = {
  datasourceId: string;
  name: string;
  description?: string;
  queryConfig: QueryConfig;
  detectTypes?: string[];
  threshold?: Record<string, unknown> | null;
  reportType?: "daily_report" | "weekly_report";
  schedule?: "daily" | "weekly";
  scheduleTime?: string;
  enabled?: boolean;
};

export async function getInsightRule(id: string): Promise<InsightRule> {
  const response = await apiClient.get(`/insights/rules/${id}`);
  return unwrapApi<InsightRule>(response.data);
}

export async function updateInsightRule(
  id: string,
  body: Partial<{
    name: string;
    description: string;
    queryConfig: QueryConfig;
    detectTypes: string[];
    threshold: Record<string, unknown> | null;
    scheduleTime: string;
    enabled: boolean;
  }>
): Promise<InsightRule> {
  const response = await apiClient.patch(`/insights/rules/${id}`, body);
  return unwrapApi<InsightRule>(response.data);
}

export async function deleteInsightRule(id: string): Promise<void> {
  await apiClient.delete(`/insights/rules/${id}`);
}

export async function runInsightRuleNow(
  id: string,
  body?: { periodStart?: string; periodEnd?: string }
): Promise<RunRuleResult> {
  const response = await apiClient.post(`/insights/rules/${id}/run`, body || {});
  return unwrapApi<RunRuleResult>(response.data);
}

export async function listInsightRecords(params?: {
  ruleId?: string;
  page?: number;
  pageSize?: number;
}): Promise<InsightRecordListResult> {
  const response = await apiClient.get("/insights/records", { params });
  return unwrapApi<InsightRecordListResult>(response.data);
}

export async function getInsightRecord(id: string): Promise<InsightRecord> {
  const response = await apiClient.get(`/insights/records/${id}`);
  return unwrapApi<InsightRecord>(response.data);
}

// ============ Suggestions ============
export async function listInsightSuggestions(params?: {
  status?: string;
  datasourceId?: string;
}): Promise<InsightSuggestionListResult> {
  const response = await apiClient.get("/insights/suggestions", { params });
  return unwrapApi<InsightSuggestionListResult>(response.data);
}

export async function discoverDatasource(datasourceId: string): Promise<DiscoverResult> {
  const response = await apiClient.post(`/insights/discover/${datasourceId}`);
  return unwrapApi<DiscoverResult>(response.data);
}

export async function acceptSuggestion(
  id: string,
  body?: Partial<{
    name: string;
    scheduleTime: string;
    detectTypes: string[];
    enabled: boolean;
  }>
): Promise<InsightRule> {
  const response = await apiClient.post(`/insights/suggestions/${id}/accept`, body || {});
  return unwrapApi<InsightRule>(response.data);
}

export async function dismissSuggestion(id: string): Promise<void> {
  await apiClient.post(`/insights/suggestions/${id}/dismiss`);
}
