import apiClient, { unwrapApi } from "./client";
import type {
  ConnectionHealthResult,
  ConnectionInfo,
  SslMode,
} from "../types/datasource";

/** GET /connections —— 当前用户的数据库连接列表（含健康状态） */
export async function listConnections(
  params: { page?: number; pageSize?: number } = {}
): Promise<{ items: ConnectionInfo[]; total: number }> {
  const response = await apiClient.get("/connections", { params });
  return unwrapApi<{ items: ConnectionInfo[]; total: number }>(response.data);
}

export async function getConnection(id: string): Promise<ConnectionInfo> {
  const response = await apiClient.get("/connections/" + id);
  return unwrapApi<ConnectionInfo>(response.data);
}

export async function updateConnection(
  id: string,
  payload: { name?: string; sslMode?: SslMode; sslCaPem?: string }
): Promise<ConnectionInfo> {
  const response = await apiClient.patch("/connections/" + id, payload);
  return unwrapApi<ConnectionInfo>(response.data);
}

/** 轮换密码：后端先验证新密码可用，再落库并重建该连接下所有表 */
export async function rotateConnectionSecret(
  id: string,
  password: string
): Promise<{ verified: boolean; datasources: Array<{ id: string; name: string; status: string }> }> {
  const response = await apiClient.post("/connections/" + id + "/rotate-secret", { password });
  return unwrapApi<{
    verified: boolean;
    datasources: Array<{ id: string; name: string; status: string }>;
  }>(response.data);
}

export async function checkConnection(id: string): Promise<ConnectionHealthResult> {
  const response = await apiClient.post("/connections/" + id + "/health");
  return unwrapApi<ConnectionHealthResult>(response.data);
}
