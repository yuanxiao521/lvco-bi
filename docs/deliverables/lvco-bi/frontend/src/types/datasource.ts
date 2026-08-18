import type { PaginationParams } from "./api";

export type DatasourceType = "csv" | "excel" | "mysql" | "postgresql";
export type DatasourceStatus = "connected" | "disconnected" | "syncing";

export interface SchemaField {
  name: string;
  dataType: string;
  nullable: boolean;
  category: "key" | "measure" | "dimension" | "time";
  displayName: string;
  sample?: unknown[];
}

export interface DataSource {
  id: string;
  userId: string;
  name: string;
  sourceType: DatasourceType;
  connectionConfig: Record<string, unknown> | null;
  filePath: string | null;
  schemaMeta: { fields: SchemaField[] } | null;
  status: DatasourceStatus;
  sizeBytes: number;
  rowCount: number;
  lastSyncedAt: string | null;
  createdAt: string;
  updatedAt: string | null;
}

export interface DataSourcePreview {
  datasourceId: string;
  columns: string[];
  rows: unknown[][];
  totalRows: number;
  previewRows: number;
}

export interface DataSourceCreatePayload {
  name: string;
  sourceType: DatasourceType;
  connectionConfig?: Record<string, unknown> | null;
  filePath?: string | null;
}

export interface DataSourceUpdatePayload {
  name?: string;
  status?: DatasourceStatus;
  schemaMeta?: { fields: SchemaField[] } | null;
}

export interface DatasourceListParams extends PaginationParams {
  type?: DatasourceType;
  status?: DatasourceStatus;
}
