import apiClient, { unwrapApi } from "./client";
import type {
  Canvas,
  CanvasBlock,
  CanvasCreatePayload,
  ChartQueryConfig,
  ChartType,
  PaginatedResult,
  QueryResult,
} from "./types";

export interface CanvasChartConfigCreatePayload {
  chartType: ChartType;
  queryConfig: ChartQueryConfig;
  blockId?: string;
}

export interface CanvasChartConfigCreateResult {
  chartConfigId: string;
  blockId?: string;
}

export async function listCanvases(
  params: { page?: number; pageSize?: number } = {}
): Promise<PaginatedResult<Canvas>> {
  const response = await apiClient.get("/canvases", { params });
  return unwrapApi<PaginatedResult<Canvas>>(response.data);
}

export async function createCanvas(payload: CanvasCreatePayload): Promise<Canvas> {
  const response = await apiClient.post("/canvases", payload);
  return unwrapApi<Canvas>(response.data);
}

export async function getCanvas(id: string): Promise<Canvas> {
  const response = await apiClient.get(`/canvases/${id}`);
  return unwrapApi<Canvas>(response.data);
}

export async function updateCanvasBlocks(
  id: string,
  blocks: CanvasBlock[]
): Promise<Canvas> {
  const response = await apiClient.put(`/canvases/${id}/blocks`, { blocks });
  return unwrapApi<Canvas>(response.data);
}

export async function updateCanvas(
  id: string,
  payload: { title: string }
): Promise<Canvas> {
  const response = await apiClient.patch(`/canvases/${id}`, payload);
  return unwrapApi<Canvas>(response.data);
}

export async function executeChartQuery(
  canvasId: string,
  config: ChartQueryConfig,
  options?: { force?: boolean }
): Promise<QueryResult> {
  // force=true 时跳过缓存读和缓存写（用户手动刷新用）
  const response = await apiClient.post(`/canvases/${canvasId}/query`, config, {
    params: options?.force ? { force: true } : undefined,
  });
  return unwrapApi<QueryResult>(response.data);
}

export async function createCanvasChartConfig(
  canvasId: string,
  payload: CanvasChartConfigCreatePayload
): Promise<string> {
  const response = await apiClient.post(
    `/canvases/${canvasId}/chart-configs`,
    payload
  );
  const data = unwrapApi<CanvasChartConfigCreateResult>(response.data);
  return data.chartConfigId;
}

export async function deleteCanvas(id: string): Promise<void> {
  await apiClient.delete(`/canvases/${id}`);
}

// POST /canvases/{canvasId}/ai-recommend
export async function recommendChartTypes(
  canvasId: string | null | undefined,
  config: Record<string, unknown>,
  datasourceId?: string | null | undefined
): Promise<{
  suggestions: Array<{
    chart_type: string;
    rationale: string;
    config: Record<string, unknown>;
    confidence: number;
  }>;
}> {
  // 有 canvasId 时走画布端点；否则用 datasource 端点（支持未创建画布的新场景）
  if (canvasId) {
    const res = await apiClient.post(`/canvases/${canvasId}/ai-recommend`, {
      current_config: config,
      datasource_id: datasourceId ?? undefined,
    });
    return res.data.data;
  }
  if (!datasourceId) {
    throw new Error("缺少数据源 ID，无法推荐图表");
  }
  const res = await apiClient.post(`/canvases/ai-recommend`, {
    current_config: config,
    datasource_id: datasourceId,
  });
  return res.data.data;
}

// POST /canvases/{canvasId}/ai-image (placeholder)
export async function generateCanvasImage(
  canvasId: string,
  description: string
): Promise<{ image_url: string | null; message: string }> {
  const res = await apiClient.post(`/canvases/${canvasId}/ai-image`, {
    description,
  });
  return res.data.data;
}

// POST /canvases/{canvasId}/pin-to-dashboard
export async function pinCanvasToDashboard(
  canvasId: string,
  payload: {
    dashboard_id: string;
    chart_config_id?: string;
    chart_config?: Record<string, unknown>;
    position?: Record<string, number>;
  }
): Promise<{ chart_id: string }> {
  const res = await apiClient.post(
    `/canvases/${canvasId}/pin-to-dashboard`,
    payload
  );
  return res.data.data;
}

// POST /canvases/{canvasId}/save-as-report
export async function saveCanvasAsReport(
  canvasId: string,
  payload: { title: string; description?: string; status?: string }
): Promise<{ report_id: string }> {
  const res = await apiClient.post(
    `/canvases/${canvasId}/save-as-report`,
    payload
  );
  return res.data.data;
}

// GET /canvases/{canvasId}/export/pdf
export async function exportCanvasPdf(canvasId: string): Promise<Blob> {
  const res = await apiClient.get(`/canvases/${canvasId}/export/pdf`, {
    responseType: "blob",
  });
  return res.data;
}
