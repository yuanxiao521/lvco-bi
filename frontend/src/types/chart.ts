import type {
  ChartType,
  FilterConfig,
  MeasureConfig,
  SortConfig,
} from "./canvas";

export interface ChartQueryConfig {
  dimensions: string[];
  measures: MeasureConfig[];
  filters?: FilterConfig[];
  chartType?: ChartType | null;
  datasourceId?: string | null;
  sort?: SortConfig | null;
  limit?: number;
}

export interface QueryResult {
  columns: string[];
  rows: Record<string, unknown>[];
  chartType: string | null;
  queryTimeMs: number;
}
