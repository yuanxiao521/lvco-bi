/**
 * 通知中心 Zustand store
 *
 * 管理通知列表、未读数，由 useNotificationStream hook 实时推送更新
 */
import { create } from "zustand";
import {
  listNotifications,
  getUnreadCount,
  markNotificationRead,
  markAllNotificationsRead,
  clearNotifications,
  type Notification,
  type NotificationListParams,
} from "../api/notifications";

interface NotificationsState {
  items: Notification[];
  unreadCount: number;
  total: number;
  loading: boolean;
  error: string | null;

  /** 加载通知列表 */
  load: (params?: NotificationListParams) => Promise<void>;

  /** 刷新未读数（轻量，不拉列表） */
  refreshUnread: () => Promise<void>;

  /** 单条标记已读（乐观更新） */
  markRead: (id: string) => Promise<void>;

  /** 全部已读 */
  markAllRead: () => Promise<void>;

  /** 清空所有 */
  clearAll: () => Promise<void>;

  /** SSE 实时推送新通知 */
  pushNotification: (notif: Notification) => void;
}

export const useNotificationsStore = create<NotificationsState>((set, get) => ({
  items: [],
  unreadCount: 0,
  total: 0,
  loading: false,
  error: null,

  load: async (params = {}) => {
    set({ loading: true, error: null });
    try {
      const result = await listNotifications(params);
      set({
        items: result.items,
        total: result.total,
        unreadCount: result.unreadCount,
        loading: false,
      });
    } catch (e) {
      set({
        error: e instanceof Error ? e.message : "加载通知失败",
        loading: false,
      });
    }
  },

  refreshUnread: async () => {
    try {
      const count = await getUnreadCount();
      set({ unreadCount: count });
    } catch {
      // 静默失败
    }
  },

  markRead: async (id: string) => {
    // 乐观更新：先更新本地状态
    set((state) => ({
      items: state.items.map((n) =>
        n.id === id ? { ...n, read: true, readAt: new Date().toISOString() } : n
      ),
      unreadCount: Math.max(0, state.unreadCount - 1),
    }));
    try {
      await markNotificationRead(id);
    } catch {
      // 失败时回滚（轻量处理，不强制）
      await get().load();
    }
  },

  markAllRead: async () => {
    set((state) => ({
      items: state.items.map((n) => ({ ...n, read: true, readAt: new Date().toISOString() })),
      unreadCount: 0,
    }));
    try {
      await markAllNotificationsRead();
    } catch {
      await get().load();
    }
  },

  clearAll: async () => {
    try {
      await clearNotifications();
      set({ items: [], unreadCount: 0, total: 0 });
    } catch {
      await get().load();
    }
  },

  pushNotification: (notif: Notification) => {
    set((state) => ({
      items: [notif, ...state.items],
      unreadCount: state.unreadCount + 1,
      total: state.total + 1,
    }));
  },
}));
