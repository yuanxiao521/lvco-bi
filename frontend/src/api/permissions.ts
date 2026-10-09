import apiClient, { extractData, unwrapApi } from "./client";

export type UserRole = "admin" | "editor" | "viewer";

export interface UserListItem {
  id: string;
  email: string;
  displayName: string;
  role: UserRole;
  avatarUrl: string | null;
  createdAt: string;
  lastLoginAt: string | null;
  datasourceCount: number;
  canvasCount: number;
  dashboardCount: number;
}

export interface UserListResult {
  items: UserListItem[];
  total: number;
  page: number;
  pageSize: number;
  pages: number;
}

export interface UserListParams {
  page?: number;
  pageSize?: number;
  search?: string;
  role?: UserRole;
}

export async function listUsers(params: UserListParams = {}): Promise<UserListResult> {
  const response = await apiClient.get("/permissions/users", { params });
  return unwrapApi<UserListResult>(extractData(response));
}

export async function updateUserRole(userId: string, role: UserRole): Promise<void> {
  const response = await apiClient.patch(`/permissions/users/${userId}/role`, { role });
  unwrapApi(extractData(response));
}

export const ROLE_LABELS: Record<UserRole, string> = {
  admin: "管理员",
  editor: "编辑者",
  viewer: "查看者",
};

export const ROLE_COLORS: Record<UserRole, string> = {
  admin: "bg-danger-light text-danger",
  editor: "bg-primary-light text-primary",
  viewer: "bg-muted text-muted-foreground",
};
