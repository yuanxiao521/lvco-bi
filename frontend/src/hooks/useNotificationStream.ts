/**
 * useNotificationStream - SSE 实时推送 hook
 *
 * 订阅后端 /api/v1/notifications/stream，收到 notification 事件时推入 store。
 * 自动重连（EventSource 原生支持），登录态变化时自动重建连接。
 */
import { useEffect, useRef } from "react";
import { useAuthStore } from "../stores/authStore";
import { useNotificationsStore } from "../stores/notificationsStore";
import { getNotificationStreamUrl } from "../api/notifications";
import type { Notification } from "../api/notifications";

export function useNotificationStream() {
  const token = useAuthStore((s) => s.accessToken);
  const pushNotification = useNotificationsStore((s) => s.pushNotification);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    // 无 token 时不订阅
    if (!token) {
      esRef.current?.close();
      esRef.current = null;
      return;
    }

    const url = getNotificationStreamUrl();
    if (!url) return;

    // 关闭旧连接
    if (esRef.current) {
      esRef.current.close();
    }

    const es = new EventSource(url);
    esRef.current = es;

    es.addEventListener("notification", (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data) as Notification;
        pushNotification(data);
      } catch {
        // 解析失败忽略
      }
    });

    es.addEventListener("error", () => {
      // EventSource 会自动重连，这里只清理引用
      // 如果连接彻底失败（如 401），readyState 变为 CLOSED
      if (es.readyState === EventSource.CLOSED) {
        esRef.current = null;
      }
    });

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [token, pushNotification]);
}
