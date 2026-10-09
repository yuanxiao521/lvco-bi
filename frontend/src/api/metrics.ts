import apiClient, { unwrapApi } from "./client";
import type {
  MetricCreatePayload,
  MetricDefinition,
  MetricImpact,
  MetricLineageEdge,
  MetricsListParams,
  MetricUpdatePayload,
  MetricVersionCompare,
  MetricVersionItem,
} from "../types/metric";

export async function listMetrics(
  params: MetricsListParams = {}
): Promise<MetricDefinition[]> {
  const response = await apiClient.get("/metrics", { params });
  return unwrapApi<MetricDefinition[]>(response.data);
}

export async function getMetric(id: string): Promise<MetricDefinition> {
  const response = await apiClient.get(`/metrics/${id}`);
  return unwrapApi<MetricDefinition>(response.data);
}

export async function createMetric(
  payload: MetricCreatePayload
): Promise<MetricDefinition> {
  const response = await apiClient.post("/metrics", payload);
  return unwrapApi<MetricDefinition>(response.data);
}

export async function updateMetric(
  id: string,
  payload: MetricUpdatePayload
): Promise<MetricDefinition> {
  const response = await apiClient.patch(`/metrics/${id}`, payload);
  return unwrapApi<MetricDefinition>(response.data);
}

export async function deleteMetric(id: string): Promise<void> {
  await apiClient.delete(`/metrics/${id}`);
}

export async function getMetricDependencies(
  id: string
): Promise<MetricDefinition[]> {
  const response = await apiClient.get(`/metrics/${id}/dependencies`);
  const data = unwrapApi<MetricDefinition[] | { items: MetricDefinition[] }>(
    response.data
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}

export async function getMetricDependents(
  id: string
): Promise<MetricDefinition[]> {
  const response = await apiClient.get(`/metrics/${id}/dependents`);
  const data = unwrapApi<MetricDefinition[] | { items: MetricDefinition[] }>(
    response.data
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}

export async function getMetricImpact(id: string): Promise<MetricImpact> {
  const response = await apiClient.get(`/metrics/${id}/impact`);
  return unwrapApi<MetricImpact>(response.data);
}

export async function getMetricLineage(
  id: string
): Promise<MetricLineageEdge[]> {
  const response = await apiClient.get(`/metrics/${id}/lineage`);
  const data = unwrapApi<MetricLineageEdge[] | { items: MetricLineageEdge[] }>(
    response.data
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}

export async function publishMetricVersion(
  id: string,
  changeNote: string
): Promise<MetricDefinition> {
  const response = await apiClient.post(`/metrics/${id}/versions`, {
    change_note: changeNote,
  });
  return unwrapApi<MetricDefinition>(response.data);
}

export async function rollbackMetricVersion(
  id: string,
  version: string
): Promise<MetricDefinition> {
  const response = await apiClient.post(`/metrics/${id}/rollback`, { version });
  return unwrapApi<MetricDefinition>(response.data);
}

export async function compareMetricVersions(
  id: string,
  fromVersion: string,
  toVersion: string
): Promise<MetricVersionCompare> {
  const response = await apiClient.get(`/metrics/${id}/versions/compare`, {
    params: { from_version: fromVersion, to_version: toVersion },
  });
  return unwrapApi<MetricVersionCompare>(response.data);
}

export async function listMetricVersions(id: string): Promise<MetricVersionItem[]> {
  const response = await apiClient.get(`/metrics/${id}/versions`);
  const data = unwrapApi<MetricVersionItem[] | { items: MetricVersionItem[] }>(
    response.data
  );
  return Array.isArray(data) ? data : (data.items ?? []);
}