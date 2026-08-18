import { NavLink, useLocation, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Gauge,
  FileText,
  Database,
  BarChart3,
  Sparkles,
  Lightbulb,
  Settings,
  ChevronRight,
  X,
  ChevronDown,
  LogOut,
  LayoutTemplate,
  Trash2,
  Bell,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useAuthStore } from "../../stores/authStore";
import { useNotificationsStore } from "../../stores/notificationsStore";
import { logout } from "../../api/auth";

interface SidebarProps {
  onClose?: () => void;
}

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "自由画布" },
  { to: "/dashboard", icon: Gauge, label: "仪表盘" },
  { to: "/report-center", icon: FileText, label: "报表中心" },
  { to: "/insights", icon: Lightbulb, label: "智能洞察" },
  { to: "/data-source", icon: Database, label: "源数据管理" },
  { to: "/statistics", icon: BarChart3, label: "统计分析" },
  { to: "/ai-chat", icon: Sparkles, label: "AI 助手" },
];

const workspaceItems = [
  { to: "/templates", icon: LayoutTemplate, label: "模板库" },
  { to: "/notifications", icon: Bell, label: "通知中心" },
  { to: "/trash", icon: Trash2, label: "回收站" },
];

const settingsItems = [
  { label: "账号设置", to: "/account-settings" },
  { label: "权限管理", to: "/permissions" },
  { label: "日志审计", to: "/audit" },
];

export default function Sidebar({ onClose }: SidebarProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const accountMenuRef = useRef<HTMLDivElement | null>(null);
  const user = useAuthStore((s) => s.user);
  const unreadCount = useNotificationsStore((s) => s.unreadCount);
  const refreshUnread = useNotificationsStore((s) => s.refreshUnread);

  // 定期刷新未读数（SSE 推送也会更新，但兜底拉一次）
  useEffect(() => {
    if (!user) return;
    refreshUnread();
    const t = setInterval(refreshUnread, 60000);
    return () => clearInterval(t);
  }, [user, refreshUnread]);

  const handleLogout = async () => {
    try {
      await logout();
    } catch {}
    useAuthStore.getState().logout();
    window.location.href = "/login";
  };

  // 点击外部关闭账号菜单
  useEffect(() => {
    if (!accountMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (accountMenuRef.current && !accountMenuRef.current.contains(e.target as Node)) {
        setAccountMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [accountMenuOpen]);

  const displayName = user?.displayName || "未登录";
  const firstChar = displayName.slice(0, 1).toUpperCase();
  const roleBadge = user?.role === "admin" ? "Admin" : user?.role === "editor" ? "Editor" : "Viewer";

  return (
    <div className="flex flex-col h-full">
      {/* Mobile close button */}
      {onClose && (
        <button onClick={onClose} className="md:hidden absolute top-3 right-3 p-1">
          <X className="w-4 h-4" />
        </button>
      )}

      <div className="flex items-center gap-2.5 px-5 h-14 border-b border-sidebar-border">
        <div className="w-8 h-8 rounded-lg flex items-center justify-center relative bg-primary-light">
          <svg viewBox="0 0 80 80" className="w-5 h-5">
            <rect x="8" y="12" width="6" height="56" rx="2" fill="#2BB5A0" />
            <rect x="14" y="64" width="58" height="4" rx="1" fill="#2BB5A0" />
            <rect x="14" y="20" width="4" height="44" rx="1" fill="#2BB5A0" />
            <rect x="68" y="20" width="4" height="44" rx="1" fill="#2BB5A0" />
            <path
              d="M18 20 C18 8, 30 4, 42 4 C54 4, 66 8, 66 20"
              stroke="#2BB5A0"
              strokeWidth="4"
              fill="none"
              strokeLinecap="round"
            />
            <rect x="22" y="52" width="4" height="12" rx="1" fill="white" opacity="0.9" />
            <rect x="28" y="46" width="4" height="18" rx="1" fill="white" opacity="0.75" />
            <rect x="34" y="40" width="4" height="24" rx="1" fill="white" />
          </svg>
        </div>
        <span className="text-[15px] font-semibold text-foreground">Lvco BI</span>
      </div>

      <nav className="flex-1 py-3 px-3 space-y-0.5 overflow-y-auto">
        {navItems.map(({ to, icon: Icon, label }) => {
          const isActive = location.pathname === to;
          return (
            <NavLink
              key={to}
              to={to}
              className={`flex items-center gap-3 px-3 py-2 rounded-[8px] text-[13px] font-medium relative transition-colors ${
                isActive
                  ? "bg-sidebar-active-bg text-sidebar-active-text"
                  : "text-card-foreground hover:bg-sidebar-hover"
              }`}
            >
              {isActive && (
                <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-r-full bg-primary" />
              )}
              <Icon className="w-4 h-4" />
              {label}
            </NavLink>
          );
        })}

        {/* 工作空间分组：模板库 / 通知中心 / 回收站 */}
        <div className="pt-3 mt-2 border-t border-sidebar-border/50">
          <div className="px-3 mb-1.5 text-[10px] font-semibold text-muted-foreground/70 tracking-wider uppercase">
            工作空间
          </div>
          {workspaceItems.map(({ to, icon: Icon, label }) => {
            const isActive = location.pathname === to;
            const showBadge = to === "/notifications" && unreadCount > 0;
            return (
              <NavLink
                key={to}
                to={to}
                className={`flex items-center gap-3 px-3 py-2 rounded-[8px] text-[13px] font-medium relative transition-colors ${
                  isActive
                    ? "bg-sidebar-active-bg text-sidebar-active-text"
                    : "text-card-foreground hover:bg-sidebar-hover"
                }`}
              >
                {isActive && (
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 rounded-r-full bg-primary" />
                )}
                <Icon className="w-4 h-4" />
                <span className="flex-1">{label}</span>
                {showBadge && (
                  <span className="min-w-[18px] h-[18px] px-1.5 flex items-center justify-center text-[10px] font-bold text-white bg-danger rounded-full">
                    {unreadCount > 99 ? "99+" : unreadCount}
                  </span>
                )}
              </NavLink>
            );
          })}
        </div>

        <details
          className="group"
          open={settingsOpen}
          onToggle={(e) => setSettingsOpen((e.target as HTMLDetailsElement).open)}
        >
          <summary className="flex items-center gap-3 px-3 py-2 rounded-[8px] text-[13px] text-card-foreground hover:bg-sidebar-hover transition-colors cursor-pointer list-none">
            <Settings className="w-4 h-4" />
            系统设置
            <ChevronRight className="w-3.5 h-3.5 ml-auto transition-transform group-open:rotate-90" />
          </summary>
          <div className="ml-7 mt-0.5 space-y-0.5">
            {settingsItems.map(({ label, to }) => (
              <NavLink
                key={to}
                to={to}
                className="block px-3 py-1.5 rounded-[6px] text-[12px] text-muted-foreground hover:bg-sidebar-hover cursor-pointer"
              >
                {label}
              </NavLink>
            ))}
          </div>
        </details>
      </nav>

      {/* 账号区域：主区域点击进设置；右侧悬停展开退出按钮 */}
      <div className="px-4 py-3 border-t border-sidebar-border">
        <div ref={accountMenuRef} className="relative group">
          <NavLink
            to="/account-settings"
            className="flex items-center gap-3 hover:opacity-80 transition-opacity"
          >
            <div className="w-8 h-8 rounded-full flex items-center justify-center text-white text-[12px] font-medium bg-primary flex-shrink-0">
              {firstChar}
            </div>
            <div className="flex-1 min-w-0 text-left">
              <div className="text-[13px] font-medium text-foreground truncate">{displayName}</div>
            </div>
            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-ai-light text-ai">
              {roleBadge}
            </span>
          </NavLink>

          {/* 悬停/点击展开的退出按钮（不与账号框冲突） */}
          <button
            onClick={() => setAccountMenuOpen((v) => !v)}
            title="账号菜单"
            aria-label="账号菜单"
            className="absolute right-0 top-1/2 -translate-y-1/2 w-6 h-6 rounded flex items-center justify-center text-muted-foreground hover:bg-sidebar-hover hover:text-danger opacity-0 group-hover:opacity-100 transition-opacity"
          >
            <ChevronDown className="w-3.5 h-3.5" />
          </button>

          {accountMenuOpen && (
            <div className="absolute bottom-full left-0 right-0 mb-1 bg-white border border-border-light rounded-[8px] shadow-lg overflow-hidden z-50">
              <button
                onClick={() => {
                  setAccountMenuOpen(false);
                  navigate("/account-settings");
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-[12px] text-card-foreground hover:bg-muted transition-colors text-left"
              >
                <Settings className="w-3.5 h-3.5" />
                账号设置
              </button>
              <div className="h-px bg-border-light" />
              <button
                onClick={() => {
                  setAccountMenuOpen(false);
                  handleLogout();
                }}
                className="w-full flex items-center gap-2 px-3 py-2 text-[12px] text-danger hover:bg-red-50 transition-colors text-left"
              >
                <LogOut className="w-3.5 h-3.5" />
                退出登录
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
