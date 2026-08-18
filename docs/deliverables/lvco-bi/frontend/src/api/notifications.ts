/**
 * 通知中心 API client
 */
import apiClient from "./client";
import type { PaginatedResult } from "../api/types";

export interface Notification {
  id: string;
  type: string;
  title: string;
  body: string;
  linkUrl: string | null;
  resourceType: string | null;
  resourceId: string | null;
  metadata: Record<string, unknown> | null;
  read: boolean;
  readAt: string | null;
  createdAt: string | null;
}

export interface NotificationListParams {
  page?: number;
  page_size?: number;
  unread_only?: boolean;
}

export interface NotificationListResult extends PaginatedResult<Notification> {
  unreadCount: number;
}

/** 分页查询通知 */
export async function listNotifications(
  params: NotificationListParams = {}
): Promise<NotificationListResult> {
  const { data } = await apiClient.get("/notifications", { params });
  return data.data;
}

/** 获取未读数 */
export async function getUnreadCount(): Promise<number> {
  const { data } = await apiClient.get("/notifications/unread_count");
  return data.data.unreadCount;
}

/** 标记单条已读 */
export async function markNotificationRead(id: string): Promise<void> {
  await apiClient.post(`/notifications/${id}/read`);
}

/** 全部已读 */
export async function markAllNotificationsRead(): Promise<number> {
  const { data } = await apiClient.post("/notifications/read_all");
  return data.data.updated;
}

/** 清空所有通知 */
export async function clearNotifications(): Promise<number> {
  const { data } = await apiClient.delete("/notifications");
  return data.data.deleted;
}

/** 获取 SSE stream URL（带 token，绝对路径指向后端） */
export function getNotificationStreamUrl(): string {
  const token = localStorage.getItem("access_token");
  if (!token) return "";
  const baseURL = apiClient.defaults.baseURL || "http://127.0.0.1:8000/api/v1";
  return `${baseURL}/notifications/stream?token=${encodeURIComponent(token)}`;
}
