import apiClient, { extractData, unwrapApi } from "./client";
import type {
  DataSource,
  DataSourcePreview,
  DataSourceUpdatePayload,
  DatasourceListParams,
  DatasourceStatus,
  DatasourceType,
  PaginatedResult,
} from "./types";

export async function listDatasources(
  params: DatasourceListParams = {}
): Promise<PaginatedResult<DataSource>> {
  const response = await apiClient.get("/datasources", { params });
  return unwrapApi<PaginatedResult<DataSource>>(response.data);
}

export async function getDatasource(id: string): Promise<DataSource> {
  const response = await apiClient.get(`/datasources/${id}`);
  return unwrapApi<DataSource>(response.data);
}

export async function uploadDatasource(
  file: File,
  name: string
): Promise<DataSource> {
  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  const response = await apiClient.post("/datasources/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
  });
  return unwrapApi<DataSource>(response.data);
}

export async function connectDatasource(payload: {
  name: string;
  sourceType: DatasourceType;
  host?: string;
  port?: number;
  dbName?: string;
  username?: string;
  password?: string;
  tableName?: string;
}): Promise<DataSource> {
  const response = await apiClient.post("/datasources/connect", payload);
  return unwrapApi<DataSource>(response.data);
}

export async function syncDatasource(id: string): Promise<DataSource> {
  const response = await apiClient.post(`/datasources/${id}/sync`);
  return unwrapApi<DataSource>(response.data);
}

export async function disconnectDatasource(id: string): Promise<DataSource> {
  const response = await apiClient.post(`/datasources/${id}/disconnect`);
  return unwrapApi<DataSource>(response.data);
}

export async function updateDatasourceSchema(
  id: string,
  payload: DataSourceUpdatePayload
): Promise<DataSource> {
  const response = await apiClient.patch(`/datasources/${id}/schema`, payload);
  return unwrapApi<DataSource>(response.data);
}

export async function deleteDatasource(id: string): Promise<void> {
  await apiClient.delete(`/datasources/${id}`);
}

export async function previewDatasource(
  id: string,
  limit: number = 20
): Promise<DataSourcePreview> {
  const response = await apiClient.get(`/datasources/${id}/preview`, {
    params: { limit },
  });
  return unwrapApi<DataSourcePreview>(response.data);
}

/** GET /datasources/{id}/tables — 列出 PG 数据源的所有 public 表 */
export async function listDatasourceTables(id: string): Promise<string[]> {
  const response = await apiClient.get(`/datasources/${id}/tables`);
  const data = unwrapApi<{ tables: string[] }>(response.data);
  return data.tables;
}

export async function testConnection(payload: {
  source_type: string;
  connection_info: Record<string, unknown>;
}): Promise<{ success: boolean; row_count?: number; error?: string; tables?: string[] }> {
  const res = await apiClient.post('/datasources/test-connection', payload)
  return res.data.data
}

export async function createDatasource(payload: {
  name: string;
  source_type: string;
  connection_info: Record<string, unknown>;
}): Promise<DataSource> {
  const response = await apiClient.post('/datasources', payload);
  return unwrapApi<DataSource>(response.data);
}

// POST /datasources/{id}/ai-clean
export async function aiCleanDatasource(
  id: string
): Promise<{
  summary: Record<string, unknown>;
  issues: Array<{
    field: string;
    issue_type: string;
    count: number;
    percentage: number;
    sample: unknown[];
    suggestion: string;
    severity: string;
  }>;
}> {
  const res = await apiClient.post(`/datasources/${id}/ai-clean`);
  return res.data.data;
}

export { extractData };

export type { DatasourceStatus, DatasourceType };
