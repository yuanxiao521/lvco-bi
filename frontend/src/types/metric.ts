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
  description?: string;
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

/** POST /metrics/draft 请求体：一句自然语言需求 → 指标草稿 */
export interface MetricDraftPayload {
  prompt: string;
  datasourceId?: string | null;
  tableRef?: string | null;
}

/** 草稿引用的字段与加工方式 */
export interface MetricDraftReference {
  field: string;
  transform: string;
}

/** 草稿校验结果：errors 非空时不允许采纳创建 */
export interface MetricDraftValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
}

/** AI 起草返回的指标草稿（不落库，用户确认后才创建） */
export interface MetricDraft {
  key: string;
  name: string;
  description: string | null;
  formula: string;
  aggKind: string | null;
  datasourceId: string | null;
  tableRef: string | null;
  references: MetricDraftReference[];
  ambiguities: string[];
  validation: MetricDraftValidation;
  sampleValue: number | null;
  sampleError: string | null;
  model: string;
}
