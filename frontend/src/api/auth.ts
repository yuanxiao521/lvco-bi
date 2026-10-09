import apiClient, { tokenStore, unwrapApi } from "./client";
import type {
  AuthTokens,
  LoginPayload,
  RegisterPayload,
  UserInfo,
} from "./types";

export async function changePassword(oldPassword: string, newPassword: string) {
  const res = await apiClient.post('/auth/change-password', { old_password: oldPassword, new_password: newPassword })
  return res.data
}

export async function updateProfile(data: { display_name?: string; email?: string }) {
  const res = await apiClient.patch('/auth/profile', data)
  return res.data.data as UserInfo
}

export interface LoginResult extends AuthTokens {
  user: UserInfo;
}

export async function login(payload: LoginPayload): Promise<LoginResult> {
  const response = await apiClient.post("/auth/login", payload);
  const data = unwrapApi<LoginResult>(response.data);
  tokenStore.set(data.accessToken, data.refreshToken ?? "");
  return data;
}

export async function register(payload: RegisterPayload): Promise<LoginResult> {
  const response = await apiClient.post("/auth/register", payload);
  const data = unwrapApi<LoginResult>(response.data);
  tokenStore.set(data.accessToken, data.refreshToken ?? "");
  return data;
}

export async function logout(): Promise<void> {
  try {
    await apiClient.post("/auth/logout");
  } finally {
    tokenStore.clear();
  }
}

export async function refreshAccessToken(): Promise<{ accessToken: string; tokenType: string }> {
  const refresh = tokenStore.getRefresh();
  if (!refresh) {
    throw new Error("没有可用的 refresh token");
  }
  const response = await apiClient.post("/auth/refresh", { refreshToken: refresh });
  return unwrapApi<{ accessToken: string; tokenType: string }>(response.data);
}

export function isAuthenticated(): boolean {
  return Boolean(tokenStore.getAccess());
}
