import { useEffect } from "react";
import {
  Bell,
  Loader2,
  Sparkles,
  AlertTriangle,
  Info,
  Check,
  Trash2,
} from "lucide-react";
import { useNotificationsStore } from "../../stores/notificationsStore";
import type { Notification } from "../../api/notifications";

const TYPE_META: Record<string, { icon: typeof Sparkles; color: string; label: string }> = {
  ai_insight: { icon: Sparkles, color: "bg-ai-light text-ai", label: "AI 洞察" },
  data_alert: { icon: AlertTriangle, color: "bg-warning-light text-warning", label: "数据告警" },
  system: { icon: Info, color: "bg-info-light text-info", label: "系统" },
};

export default function NotificationsPage() {
  const { items, unreadCount, loading, load, markRead, markAllRead, clearAll } =
    useNotificationsStore();

  useEffect(() => {
    load({ page_size: 50 });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleMarkRead = async (id: string) => {
    await markRead(id);
  };

  const handleMarkAllRead = async () => {
    await markAllRead();
  };

  const handleClearAll = async () => {
    if (window.confirm("确定清空所有通知？")) {
      await clearAll();
    }
  };

  return (
    <div className="flex-1 p-6 space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[17px] font-semibold text-foreground flex items-center gap-2">
            <Bell className="w-5 h-5 text-primary" />
            通知中心
            {unreadCount > 0 && (
              <span className="ml-2 px-2 py-0.5 rounded-full text-[11px] font-medium bg-danger text-white">
                {unreadCount} 未读
              </span>
            )}
          </h1>
          <p className="text-[12px] text-muted-foreground mt-1">
            AI 洞察日报、数据告警与系统消息
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleMarkAllRead}
            disabled={unreadCount === 0}
            className="px-3 py-1.5 text-[12px] border border-border rounded-md text-card-foreground hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
          >
            <Check className="w-3 h-3" />
            全部已读
          </button>
          <button
            onClick={handleClearAll}
            disabled={items.length === 0}
            className="px-3 py-1.5 text-[12px] border border-border rounded-md text-card-foreground hover:bg-muted disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1"
          >
            <Trash2 className="w-3 h-3" />
            清空
          </button>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground text-[13px]">
          <Loader2 className="w-4 h-4 animate-spin mr-2" />
          加载中...
        </div>
      ) : items.length === 0 ? (
        <div className="bg-white rounded-[10px] border border-border-light p-16 text-center">
          <Bell className="w-14 h-14 mx-auto mb-3 text-muted-foreground/40" />
          <h3 className="text-[14px] font-medium text-foreground mb-1">暂无通知</h3>
          <p className="text-[12px] text-muted-foreground">
            AI 洞察日报、数据告警、系统消息都会显示在这里
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((it, idx) => (
            <div key={it.id} className={`animate-slide-up stagger-${idx + 1}`}>
              <NotificationRow item={it} onRead={handleMarkRead} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function NotificationRow({
  item,
  onRead,
}: {
  item: Notification;
  onRead: (id: string) => void;
}) {
  const meta = TYPE_META[item.type] ?? TYPE_META.system;
  const Icon = meta.icon;
  const handleClick = () => {
    if (!item.read) {
      onRead(item.id);
    }
    if (item.linkUrl) {
      window.location.href = item.linkUrl;
    }
  };

  return (
    <div
      className={`bg-white rounded-[10px] border p-4 transition-colors cursor-pointer ${
        item.read ? "border-border-light" : "border-primary/30 bg-primary-light/10"
      }`}
      onClick={handleClick}
    >
      <div className="flex items-start gap-3">
        <div className={`w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0 ${meta.color}`}>
          <Icon className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${meta.color}`}>
              {meta.label}
            </span>
            {!item.read && <span className="w-1.5 h-1.5 rounded-full bg-danger flex-shrink-0" />}
            <span className="text-[11px] text-muted-foreground ml-auto flex-shrink-0">
              {item.createdAt
                ? new Date(item.createdAt).toLocaleString("zh-CN")
                : ""}
            </span>
          </div>
          <h3 className={`text-[13px] mb-1 ${item.read ? "text-card-foreground" : "text-foreground font-medium"}`}>
            {item.title}
          </h3>
          <p className="text-[12px] text-muted-foreground">{item.body}</p>
        </div>
      </div>
    </div>
  );
}
