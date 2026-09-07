import type { PaginationParams } from "./api";

export type MetricFormulaType = "basic" | "derived";

export interface MetricDefinition {
  id: string;
  key: string;
  name: string;
  formula: string;
  formulaType: MetricFormulaType;
  dependsOnMetricIds: string[];
  version: string;
  /** 绑定数据源；为 null 表示通用模板指标（需绑定字段才能解析） */
  datasourceId?: string | null;
  aggKind?: string | null;
  tableRef?: string | null;
}

export interface MetricCreatePayload {
  key: string;
  name: string;
  formula?: string;
  formulaType?: MetricFormulaType;
  dependsOnMetricIds?: string[];
  aggKind?: string;
  datasourceId?: string;
  tableRef?: string;
  /** 免手写 SQL：选字段 + 聚合方式自动生成公式 */
  sourceField?: string;
  agg?: string;
}

export interface MetricUpdatePayload {
  name?: string;
  description?: string;
  formula?: string;
  aggKind?: string;
  tableRef?: string;
  datasourceId?: string;
  active?: boolean;
}

export interface MetricsListParams extends PaginationParams {
  formula_type?: MetricFormulaType;
}

export interface MetricImpact {
  metric_id: string;
  name: string;
  dependents_count: number;
  dashboards_count: number;
  users_count: number;
}

export interface MetricLineageEdge {
  source_field: string;
  transform: string;
  target_field: string;
  dataset_name: string;
}

export interface MetricVersionItem {
  version: string;
  created_at: string;
  change_note?: string;
}

export interface MetricVersionDiff {
  field: string;
  from: unknown;
  to: unknown;
}

export interface MetricVersionCompare {
  from_version: string;
  to_version: string;
  changes: MetricVersionDiff[];
}