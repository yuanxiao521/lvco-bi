import apiClient, { unwrapApi } from "./client";
import type {
  DashboardAddChartPayload,
  DashboardCreatePayload,
  DashboardDataResult,
  DashboardDetail,
  DashboardLayoutPayload,
  DashboardShareResult,
  DashboardSummary,
  PaginatedResult,
} from "./types";

export async function listDashboards(
  params: { page?: number; pageSize?: number; search?: string } = {}
): Promise<PaginatedResult<DashboardSummary>> {
  const response = await apiClient.get("/dashboards", { params });
  return unwrapApi<PaginatedResult<DashboardSummary>>(response.data);
}

export async function createDashboard(
  payload: DashboardCreatePayload
): Promise<DashboardSummary> {
  const response = await apiClient.post("/dashboards", payload);
  return unwrapApi<DashboardSummary>(response.data);
}

export async function getDashboard(id: string): Promise<DashboardDetail> {
  const response = await apiClient.get(`/dashboards/${id}`);
  return unwrapApi<DashboardDetail>(response.data);
}

export async function updateDashboardLayout(
  id: string,
  payload: DashboardLayoutPayload
): Promise<DashboardSummary> {
  const response = await apiClient.put(`/dashboards/${id}/layout`, payload);
  return unwrapApi<DashboardSummary>(response.data);
}

export async function addDashboardChart(
  id: string,
  payload: DashboardAddChartPayload
): Promise<{ chartId: string; dashboardId: string; title: string | null; position: Record<string, unknown> | null }> {
  const response = await apiClient.post(`/dashboards/${id}/charts`, payload);
  return unwrapApi(response.data);
}

export async function removeDashboardChart(
  dashboardId: string,
  chartId: string
): Promise<void> {
  await apiClient.delete(`/dashboards/${dashboardId}/charts/${chartId}`);
}

export async function getDashboardData(id: string): Promise<DashboardDataResult> {
  const response = await apiClient.get(`/dashboards/${id}/data`);
  return unwrapApi<DashboardDataResult>(response.data);
}

export async function refreshDashboard(id: string): Promise<DashboardDataResult> {
  const response = await apiClient.post(`/dashboards/${id}/refresh`);
  return unwrapApi<DashboardDataResult>(response.data);
}

export async function shareDashboard(id: string): Promise<DashboardShareResult> {
  const response = await apiClient.post(`/dashboards/${id}/share`);
  return unwrapApi<DashboardShareResult>(response.data);
}

export async function deleteDashboard(id: string): Promise<void> {
  await apiClient.delete(`/dashboards/${id}`);
}
