import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Plus,
  Search,
  BarChart3,
  User,
  Clock,
  X,
  Loader2,
  Trash2,
} from "lucide-react";
import { useQuery } from "../../hooks/useQuery";
import {
  listDashboards,
  createDashboard,
  deleteDashboard,
} from "../../api/dashboards";
import { useToast } from "../../components/ui/Toast";
import type { PaginatedResult } from "../../api/types";
import type { DashboardSummary } from "../../types/dashboard";
import { useAuthStore } from "../../stores/authStore";

function formatRelative(input: string | null | undefined): string {
  if (!input) return "未知";
  const target = new Date(input);
  if (Number.isNaN(target.getTime())) return "未知";
  const diffMs = Date.now() - target.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "刚刚";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour}小时前`;
  const diffDay = Math.floor(diffHour / 24);
  if (diffDay < 30) return `${diffDay}天前`;
  const diffMonth = Math.floor(diffDay / 30);
  if (diffMonth < 12) return `${diffMonth}个月前`;
  const diffYear = Math.floor(diffDay / 365);
  return `${diffYear}年前`;
}

function ModalShell({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
        aria-hidden
      />
      <div className="relative bg-white rounded-lg shadow-xl w-[min(480px,92vw)] max-h-[88vh] flex flex-col">
        <div className="px-5 py-4 border-b border-border-light flex items-center justify-between">
          <h3 className="text-[15px] font-semibold text-foreground">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
            title="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="px-5 py-4 overflow-auto flex-1">{children}</div>
        {footer ? (
          <div className="px-5 py-3 border-t border-border-light flex items-center justify-end gap-2">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default function DashboardList() {
  const navigate = useNavigate();
  const toast = useToast();
  const currentUser = useAuthStore((s) => s.user);
  const ownerFallback = currentUser?.displayName ?? "我";

  const { data, loading, error, refetch } = useQuery<
    PaginatedResult<DashboardSummary>
  >(() => listDashboards({ page: 1, pageSize: 100 }), []);

  const [searchTerm, setSearchTerm] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const dashboards: DashboardSummary[] = data?.items ?? [];
  const total = data?.total ?? 0;

  const filtered = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    if (!term) return dashboards;
    return dashboards.filter((d) => d.title.toLowerCase().includes(term));
  }, [dashboards, searchTerm]);

  const openCreate = () => {
    setNewTitle("");
    setCreateError(null);
    setCreateOpen(true);
  };

  const closeCreate = () => {
    if (creating) return;
    setCreateOpen(false);
  };

  const handleCreate = async () => {
    const title = newTitle.trim();
    if (!title) {
      setCreateError("请输入仪表盘标题");
      return;
    }
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createDashboard({ title });
      setCreateOpen(false);
      setNewTitle("");
      await refetch();
      navigate(`/dashboard/${created.id}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "创建失败";
      setCreateError(msg);
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: string, title: string) => {
    if (!window.confirm(`确定删除仪表盘「${title}」？可前往回收站恢复。`)) return;
    setDeletingId(id);
    try {
      await deleteDashboard(id);
      toast.success(`已删除「${title}」`);
      await refetch();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "删除失败";
      toast.error(msg);
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <>
      <header className="h-14 flex items-center justify-between px-6 border-b bg-white flex-shrink-0 border-border">
        <h1 className="text-[18px] font-semibold text-foreground">仪表盘</h1>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={openCreate}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[12px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            新建仪表盘
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-auto p-6 space-y-6">
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-white rounded-lg p-5 shadow-card border border-border-light">
            <div className="flex items-start justify-between">
              <div>
                <p className="text-[13px] text-muted-foreground mb-1">
                  仪表盘总数
                </p>
                <p className="text-[28px] font-semibold text-foreground">
                  {loading ? "—" : total}
                </p>
              </div>
              <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-primary-light">
                <LayoutDashboard className="w-5 h-5 text-primary" />
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg p-5 shadow-card border border-border-light">
            <div className="flex items-start justify-between">
              <div>
                <p className="text-[13px] text-muted-foreground mb-1">图表总数</p>
                <p className="text-[28px] font-semibold text-foreground">
                  {loading
                    ? "—"
                    : dashboards.reduce(
                        (sum, d) => sum + (d.chartCount ?? 0),
                        0,
                      )}
                </p>
              </div>
              <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-ai-light">
                <BarChart3 className="w-5 h-5 text-ai" />
              </div>
            </div>
          </div>

          <div className="bg-white rounded-lg p-5 shadow-card border border-border-light">
            <div className="flex items-start justify-between">
              <div>
                <p className="text-[13px] text-muted-foreground mb-1">
                  最近更新
                </p>
                <p className="text-[18px] font-semibold text-foreground mt-1">
                  {loading
                    ? "—"
                    : formatRelative(
                        dashboards
                          .map((d) => d.updatedAt)
                          .filter((x): x is string => Boolean(x))
                          .sort()
                          .slice(-1)[0] ?? null,
                      )}
                </p>
              </div>
              <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-success-light">
                <Clock className="w-5 h-5 text-success" />
              </div>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="relative flex-1 max-w-sm">
            <Search className="w-4 h-4 text-muted-foreground absolute left-3 top-1/2 -translate-y-1/2" />
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="搜索仪表盘标题"
              className="w-full h-9 pl-9 pr-3 rounded-lg border border-border bg-white text-[13px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary"
            />
          </div>
          {loading ? (
            <span className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              加载中...
            </span>
          ) : error ? (
            <span className="text-[12px] text-danger">加载失败：{error.message}</span>
          ) : (
            <span className="text-[12px] text-muted-foreground">
              共 {filtered.length} 个仪表盘
            </span>
          )}
        </div>

        {filtered.length === 0 && !loading && !error ? (
          <div className="bg-white rounded-lg shadow-card border border-border-light py-16 flex flex-col items-center justify-center text-center">
            <LayoutDashboard className="w-10 h-10 text-muted-foreground mb-3" />
            <p className="text-[14px] text-foreground font-medium">
              {searchTerm ? "未找到匹配的仪表盘" : "暂无仪表盘"}
            </p>
            <p className="text-[12px] text-muted-foreground mt-1 mb-4">
              {searchTerm
                ? "尝试更换搜索关键词"
                : "点击右上角「新建仪表盘」开始创建"}
            </p>
            {!searchTerm && (
              <button
                type="button"
                onClick={openCreate}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-[8px] text-[12px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors"
              >
                <Plus className="w-3.5 h-3.5" />
                新建仪表盘
              </button>
            )}
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-5">
            {filtered.map((dashboard) => (
              <div
                key={dashboard.id}
                className="relative bg-white rounded-lg p-5 shadow-card border border-border-light hover:shadow-float transition-shadow group"
              >
                <Link
                  to={`/dashboard/${dashboard.id}`}
                  className="block"
                >
                  <div className="flex items-start justify-between mb-3 pr-8">
                    <h3 className="text-[15px] font-semibold text-foreground line-clamp-2">
                      {dashboard.title}
                    </h3>
                  </div>

                  {dashboard.description ? (
                    <p className="text-[12px] text-muted-foreground mb-4 line-clamp-2">
                      {dashboard.description}
                    </p>
                  ) : (
                    <p className="text-[12px] text-muted-foreground mb-4 italic">
                      暂无描述
                    </p>
                  )}

                  <div className="flex items-center justify-between text-[11px] text-muted-foreground">
                    <div className="flex items-center gap-3">
                      <span className="flex items-center gap-1">
                        <BarChart3 className="w-3 h-3" />
                        {dashboard.chartCount ?? 0} 图表
                      </span>
                      <span className="flex items-center gap-1">
                        <Clock className="w-3 h-3" />
                        {formatRelative(dashboard.updatedAt)}
                      </span>
                    </div>
                    <span className="flex items-center gap-1">
                      <User className="w-3 h-3" />
                      {dashboard.ownerName ?? ownerFallback}
                    </span>
                  </div>
                </Link>

                {/* 删除按钮 */}
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    handleDelete(dashboard.id, dashboard.title);
                  }}
                  disabled={deletingId === dashboard.id}
                  className="absolute top-3 right-3 p-1.5 rounded-md text-muted-foreground hover:text-danger hover:bg-danger-light opacity-0 group-hover:opacity-100 transition-all disabled:opacity-50"
                  title="删除仪表盘"
                >
                  {deletingId === dashboard.id ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Trash2 className="w-4 h-4" />
                  )}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      <ModalShell
        open={createOpen}
        onClose={closeCreate}
        title="新建仪表盘"
        footer={
          <>
            <button
              type="button"
              onClick={closeCreate}
              disabled={creating}
              className="h-8 px-3 rounded-lg border border-border bg-white text-[13px] text-card-foreground hover:bg-muted transition-colors disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleCreate}
              disabled={creating}
              className="h-8 px-3 rounded-lg bg-primary text-white text-[13px] hover:bg-primary-hover transition-colors disabled:opacity-50 flex items-center gap-1.5"
            >
              {creating ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Plus className="w-3.5 h-3.5" />
              )}
              创建
            </button>
          </>
        }
      >
        <label className="block">
          <span className="text-[13px] font-medium text-card-foreground">
            仪表盘标题
          </span>
          <input
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !creating) {
                e.preventDefault();
                handleCreate();
              }
            }}
            placeholder="例如：销售总览仪表盘"
            autoFocus
            className="mt-2 w-full h-9 px-3 rounded-lg border border-border bg-white text-[13px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary"
          />
        </label>
        {createError ? (
          <p className="mt-2 text-[12px] text-danger">{createError}</p>
        ) : null}
      </ModalShell>
    </>
  );
}