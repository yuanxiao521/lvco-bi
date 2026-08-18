import type { ChartType } from "./canvas";
import type { ChartQueryConfig, QueryResult } from "./chart";

export interface DashboardSummary {
  id: string;
  title: string;
  description: string | null;
  chartCount: number;
  createdAt: string | null;
  updatedAt: string | null;
  ownerName?: string | null;
  ownerId?: string | null;
}

export interface DashboardChartConfig {
  id: string;
  title: string | null;
  chartType: ChartType | null;
  config: ChartQueryConfig | null;
  position: Record<string, unknown> | null;
}

export interface DashboardDetail {
  id: string;
  title: string;
  description: string | null;
  layout: Record<string, unknown>[] | null;
  chartConfigs: DashboardChartConfig[];
  createdAt: string | null;
  updatedAt: string | null;
  ownerName?: string | null;
  ownerId?: string | null;
}

export interface DashboardCreatePayload {
  title: string;
  description?: string | null;
}

export interface DashboardLayoutPayload {
  layout: Record<string, unknown>[];
}

export interface DashboardAddChartPayload {
  chartConfigId: string;
  title?: string | null;
  position?: Record<string, unknown> | null;
}

export interface DashboardChartItem {
  chartId: string;
  title: string | null;
  chartType: ChartType;
  renderConfig?: { renderer?: string; palette?: string };
  dimensions: string[];
  measures: Array<{ field: string; agg: string }>;
  data: QueryResult;
}

export interface DashboardChartErrorItem {
  chartId: string;
  title: string | null;
  error: string;
}

export type DashboardChartEntry = DashboardChartItem | DashboardChartErrorItem;

export interface DashboardDataResult {
  dashboardId: string;
  layout: Record<string, unknown>[] | null;
  charts: DashboardChartEntry[];
}

export interface DashboardShareResult {
  shareToken: string;
  shareUrl: string;
  expiresAt: string;
}
