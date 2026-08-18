import { useMemo, useRef, useState } from "react";
import {
  Plus,
  Search,
  ChevronDown,
  Eye,
  Pencil,
  Download,
  Clock,
  User,
  Trash2,
  X,
  Loader2,
  CheckCircle2,
} from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "../../hooks/useQuery";
import {
  createReport,
  deleteReport,
  downloadReport,
  getReport,
  listReports,
  updateReport,
} from "../../api/reports";
import type {
  PaginatedResult,
  Report,
  ReportSourceType,
  ReportStatus,
  ReportListParams,
} from "../../api/types";
import type { CanvasBlock } from "../../api/types";
import { useAuthStore } from "../../stores/authStore";
import { Check } from "lucide-react";

const REPORT_TRANSFER_KEY = "lvco:report:transfer";

type TabKey = "all" | ReportStatus;

interface TabDef {
  key: TabKey;
  label: string;
  status?: ReportStatus;
}

const TABS: TabDef[] = [
  { key: "all", label: "全部" },
  { key: "draft", label: "草稿", status: "draft" },
  { key: "published", label: "已发布", status: "published" },
  { key: "shared", label: "已分享", status: "shared" },
  { key: "archived", label: "已归档", status: "archived" },
];

const STATUS_BADGE: Record<
  ReportStatus,
  { label: string; className: string }
> = {
  draft: {
    label: "草稿",
    className: "bg-warning-light text-warning",
  },
  published: {
    label: "已发布",
    className: "bg-success-light text-success",
  },
  shared: {
    label: "已分享",
    className: "bg-ai-light text-ai",
  },
  archived: {
    label: "已归档",
    className: "bg-muted text-muted-foreground",
  },
};

const SOURCE_TYPE_BADGE: Record<
  ReportSourceType,
  { label: string; className: string }
> = {
  canvas: {
    label: "画布",
    className: "bg-primary-light text-primary",
  },
  dashboard: {
    label: "仪表盘",
    className: "bg-ai-light text-ai",
  },
  manual: {
    label: "手动",
    className: "bg-muted text-muted-foreground",
  },
  ai_insight: {
    label: "AI 日报",
    className: "bg-warning-light text-warning",
  },
};

function formatRelative(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  if (diff < 0) return new Date(iso).toLocaleDateString("zh-CN");
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
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

export default function ReportCenter() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [activeTabKey, setActiveTabKey] = useState<TabKey>("all");
  const [searchTerm, setSearchTerm] = useState("");
  const [sourceTypeFilter, setSourceTypeFilter] = useState<string | null>(
    searchParams.get("sourceType") || null
  );
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [showSourceTypeDropdown, setShowSourceTypeDropdown] = useState(false);
  const [showStatusDropdown, setShowStatusDropdown] = useState(false);

  // 标题内联编辑
  const [editingReportId, setEditingReportId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const editInputRef = useRef<HTMLInputElement>(null);

  const handleEdit = async (report: Report) => {
    try {
      const detail = await getReport(report.id);
      const blocks: CanvasBlock[] = detail.snapshotBlocks?.blocks ?? [];
      // 优先从快照中读取数据源 ID，避免 canvas ID 与实际数据源不匹配
      let datasourceId: string | null = (detail.snapshotBlocks as Record<string, unknown> | null)?.datasourceId as string ?? null;
      if (!datasourceId && report.sourceId) {
        try {
          const { getCanvas } = await import("../../api/canvases");
          const canvas = await getCanvas(report.sourceId);
          datasourceId = canvas.datasourceId ?? null;
        } catch { /* 画布可能已删除，忽略 */ }
      }
      localStorage.setItem(REPORT_TRANSFER_KEY, JSON.stringify({ blocks, reportId: report.id, title: report.title, datasourceId }));
    } catch {
      localStorage.setItem(REPORT_TRANSFER_KEY, JSON.stringify({ blocks: [], reportId: report.id, title: report.title, datasourceId: null }));
    }
    navigate("/");
  };

  const activeTab = useMemo(
    () => TABS.find((t) => t.key === activeTabKey) ?? TABS[0],
    [activeTabKey]
  );

  const listFn = useMemo(
    () => () => {
      const params: ReportListParams = {};
      if (activeTab.status) params.status = activeTab.status;
      if (statusFilter) params.status = statusFilter as ReportStatus;
      if (sourceTypeFilter) params.sourceType = sourceTypeFilter as ReportSourceType;
      if (searchTerm.trim()) params.search = searchTerm.trim();
      return listReports(params);
    },
    [activeTab, searchTerm, sourceTypeFilter, statusFilter]
  );

  const { data, loading, error, refetch } = useQuery<
    PaginatedResult<Report>
  >(listFn, [activeTabKey, searchTerm]);

  const reports = data?.items ?? [];

  const authUser = useAuthStore((s) => s.user);
  const ownerFallback = authUser?.displayName ?? "我";

  const [pendingDelete, setPendingDelete] = useState<Report | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [exportingId, setExportingId] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createSuccess, setCreateSuccess] = useState<string | null>(null);

  const openCreateModal = () => {
    setShowCreateModal(true);
    setNewTitle("");
    setCreateError(null);
    setCreateSuccess(null);
  };

  const closeCreateModal = () => {
    if (creating) return;
    setShowCreateModal(false);
    setCreateError(null);
    setCreateSuccess(null);
  };

  const handleCreate = async () => {
    const title = newTitle.trim();
    if (!title) {
      setCreateError("请输入报表标题");
      return;
    }
    setCreating(true);
    setCreateError(null);
    setCreateSuccess(null);
    try {
      const created = await createReport({
        title,
        sourceType: "manual" as ReportSourceType,
        sourceId: null,
      });
      setCreateSuccess(`报表已创建：${created.title}`);
      setNewTitle("");
      await refetch();
    } catch (e) {
      const msg =
        e instanceof Error
          ? e.message
          : typeof e === "string"
            ? e
            : "创建报表失败";
      setCreateError(msg);
    } finally {
      setCreating(false);
    }
  };

  const openDeleteConfirm = (report: Report) => {
    setPendingDelete(report);
    setDeleteError(null);
  };

  const closeDeleteConfirm = () => {
    if (deleting) return;
    setPendingDelete(null);
    setDeleteError(null);
  };

  const handleConfirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteReport(pendingDelete.id);
      setPendingDelete(null);
      await refetch();
    } catch (e) {
      const msg =
        e instanceof Error
          ? e.message
          : typeof e === "string"
            ? e
            : "删除报表失败";
      setDeleteError(msg);
    } finally {
      setDeleting(false);
    }
  };

  const handleDownload = async (report: Report) => {
    if (exportingId) return;
    setExportingId(report.id);
    setExportError(null);
    try {
      await downloadReport(report.id, report.title);
    } catch (e) {
      const msg =
        e instanceof Error
          ? e.message
          : typeof e === "string"
            ? e
            : "导出报表失败";
      setExportError(`${report.title}：${msg}`);
    } finally {
      setExportingId(null);
    }
  };

  return (
    <div className="flex-1 flex flex-col min-w-0">
      <header className="h-16 bg-white border-b border-border flex items-center justify-between px-6 flex-shrink-0">
        <h1 className="text-[17px] font-semibold text-foreground">
          报表中心
        </h1>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={openCreateModal}
            className="flex items-center gap-1.5 px-3.5 h-9 rounded-[6px] text-[13px] font-medium text-white bg-primary"
          >
            <Plus className="w-3.5 h-3.5" />
            新建报表
          </button>
        </div>
      </header>

      <div className="flex-1 p-6 overflow-auto">
        <div className="flex items-center gap-1 border-b border-border-light mb-5">
          {TABS.map((tab) => {
            const isActive = tab.key === activeTabKey;
            const count = isActive ? data?.total ?? 0 : 0;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => setActiveTabKey(tab.key)}
                className={`px-4 py-2.5 text-[13px] font-medium transition-colors border-b-2 -mb-px ${
                  isActive
                    ? "text-primary border-primary"
                    : "text-muted-foreground border-transparent hover:text-card-foreground"
                }`}
              >
                {tab.label}({count})
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-3 mb-5">
          <div className="relative flex-1 max-w-[320px]">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="搜索报表..."
              className="h-9 w-full pl-9 pr-3 text-[13px] bg-white border border-border rounded-[6px] outline-none focus:border-primary focus:ring-1 focus:ring-ring transition-colors placeholder:text-muted-foreground"
            />
          </div>
          <div className="relative">
            <button className="h-9 px-3.5 text-[13px] bg-white border border-border rounded-[6px] text-card-foreground hover:bg-muted flex items-center gap-2 transition-colors"
              onClick={() => { setShowSourceTypeDropdown(!showSourceTypeDropdown); setShowStatusDropdown(false); }}
              onBlur={() => setTimeout(() => setShowSourceTypeDropdown(false), 200)}
            >
              {sourceTypeFilter === 'canvas' ? '画布' : sourceTypeFilter === 'dashboard' ? '仪表盘' : sourceTypeFilter === 'manual' ? '手动' : sourceTypeFilter === 'ai_insight' ? 'AI日报' : '全部类型'}
              <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" />
            </button>
            {showSourceTypeDropdown ? (
              <div className="absolute top-full left-0 mt-1 w-32 bg-white border border-border rounded-md shadow-lg z-50">
                {[{v:null,l:'全部类型'},{v:'canvas',l:'画布'},{v:'dashboard',l:'仪表盘'},{v:'manual',l:'手动'},{v:'ai_insight',l:'AI日报'}].map(o => (
                  <div key={o.v??'all'} className="px-3 py-2 text-[13px] hover:bg-muted cursor-pointer" onMouseDown={() => setSourceTypeFilter(o.v)}>
                    {o.l}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
          <div className="relative">
            <button className="h-9 px-3.5 text-[13px] bg-white border border-border rounded-[6px] text-card-foreground hover:bg-muted flex items-center gap-2 transition-colors"
              onClick={() => { setShowStatusDropdown(!showStatusDropdown); setShowSourceTypeDropdown(false); }}
              onBlur={() => setTimeout(() => setShowStatusDropdown(false), 200)}
            >
              {statusFilter === 'draft' ? '草稿' : statusFilter === 'published' ? '已发布' : statusFilter === 'shared' ? '已分享' : statusFilter === 'archived' ? '已归档' : '全部状态'}
              <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" />
            </button>
            {showStatusDropdown ? (
              <div className="absolute top-full left-0 mt-1 w-32 bg-white border border-border rounded-md shadow-lg z-50">
                {[{v:null,l:'全部状态'},{v:'draft',l:'草稿'},{v:'published',l:'已发布'},{v:'shared',l:'已分享'},{v:'archived',l:'已归档'}].map(o => (
                  <div key={o.v??'all'} className="px-3 py-2 text-[13px] hover:bg-muted cursor-pointer" onMouseDown={() => setStatusFilter(o.v)}>
                    {o.l}
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>

        {exportError ? (
          <div className="mb-4 px-4 py-2 rounded-md bg-danger-light text-danger text-[13px]">
            导出失败：{exportError}
          </div>
        ) : null}

        {error ? (
          <div className="mb-4 px-4 py-3 rounded-md bg-danger-light text-danger text-[13px] flex items-center justify-between">
            <span>加载报表失败：{error.message}</span>
            <button
              type="button"
              onClick={() => void refetch()}
              className="ml-4 px-3 py-1 rounded text-[12px] font-medium text-white bg-primary hover:bg-primary-hover"
            >
              重试
            </button>
          </div>
        ) : null}

        {loading && reports.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-muted-foreground bg-white rounded-[10px] shadow-card">
            <span className="inline-flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在加载报表...
            </span>
          </div>
        ) : reports.length === 0 ? (
          <div className="px-4 py-12 text-center text-[13px] text-muted-foreground bg-white rounded-[10px] shadow-card">
            暂无报表，点击右上角「新建报表」创建第一个吧。
          </div>
        ) : (
          <div className="grid grid-cols-3 gap-5">
            {reports.map((report) => {
              const statusBadge = STATUS_BADGE[report.status];
              const sourceBadge = SOURCE_TYPE_BADGE[report.sourceType];
              const isExporting = exportingId === report.id;
              return (
                <div
                  key={report.id}
                  className="relative bg-card rounded-[10px] shadow-card p-5 flex flex-col gap-3 hover:shadow-float transition-shadow"
                >
                  <div className="flex items-center justify-between pr-2">
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-[11.5px] font-medium ${sourceBadge.className}`}
                    >
                      {sourceBadge.label}
                    </span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-[11.5px] font-medium ${statusBadge.className}`}
                    >
                      {statusBadge.label}
                    </span>
                  </div>

                  <div>
                    {editingReportId === report.id ? (
                      <form
                        className="flex items-center gap-1"
                        onSubmit={async (e) => {
                          e.preventDefault();
                          const newTitle = editingTitle.trim() || report.title;
                          setEditingReportId(null);
                          try {
                            await updateReport(report.id, { title: newTitle });
                            refetch();
                          } catch {}
                        }}
                      >
                        <input
                          ref={editInputRef}
                          value={editingTitle}
                          onChange={(e) => setEditingTitle(e.target.value)}
                          className="text-[14px] font-semibold text-foreground bg-transparent border-b-2 border-primary outline-none px-1 py-0 w-[180px]"
                          autoFocus
                          onBlur={async () => {
                            setEditingReportId(null);
                            const newTitle = editingTitle.trim() || report.title;
                            try {
                              await updateReport(report.id, { title: newTitle });
                              refetch();
                            } catch {}
                          }}
                        />
                        <button type="submit" className="text-primary">
                          <Check className="w-3.5 h-3.5" />
                        </button>
                      </form>
                    ) : (
                      <div className="flex items-center gap-1.5 group/title">
                        <h3 className="text-[14px] font-semibold text-foreground mb-1">
                          {report.title}
                        </h3>
                        <Pencil
                          className="w-3 h-3 text-muted-foreground cursor-pointer hover:text-primary opacity-0 group-hover/title:opacity-100 transition-opacity"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingReportId(report.id);
                            setEditingTitle(report.title);
                            setTimeout(() => editInputRef.current?.select(), 50);
                          }}
                        />
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-3 text-[12px] text-muted-foreground">
                    <span className="flex items-center gap-1">
                      <User className="w-3 h-3" />
                      {ownerFallback}
                    </span>
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {formatRelative(report.updatedAt)}
                    </span>
                  </div>

                  <div className="flex items-center gap-1 pt-2 border-t border-border-light">
                    <button
                      onClick={() => navigate(`/report-center/${report.id}`)}
                      className="flex items-center gap-1 px-2.5 py-1.5 rounded-[4px] text-[12px] text-muted-foreground hover:text-primary hover:bg-primary-light transition-colors"
                    >
                      <Eye className="w-3.5 h-3.5" />
                      查看
                    </button>
                    <button
                      onClick={() => void handleEdit(report)}
                      className="flex items-center gap-1 px-2.5 py-1.5 rounded-[4px] text-[12px] text-muted-foreground hover:text-primary hover:bg-primary-light transition-colors"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                      编辑
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDownload(report)}
                      disabled={isExporting}
                      className="flex items-center gap-1 px-2.5 py-1.5 rounded-[4px] text-[12px] text-muted-foreground hover:text-primary hover:bg-primary-light transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                    >
                      {isExporting ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : (
                        <Download className="w-3.5 h-3.5" />
                      )}
                      {isExporting ? "导出中" : "下载"}
                    </button>
                    <button
                      type="button"
                      onClick={() => openDeleteConfirm(report)}
                      disabled={isExporting}
                      className="ml-auto flex items-center gap-1 px-2.5 py-1.5 rounded-[4px] text-[12px] text-muted-foreground hover:text-danger hover:bg-danger-light transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      删除
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      <ModalShell
        open={Boolean(pendingDelete)}
        onClose={closeDeleteConfirm}
        title="确认删除"
        footer={
          <>
            <button
              type="button"
              onClick={closeDeleteConfirm}
              disabled={deleting}
              className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void handleConfirmDelete()}
              disabled={deleting}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-danger hover:opacity-90 transition-colors shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {deleting ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              删除
            </button>
          </>
        }
      >
        <div className="space-y-3 text-[13px]">
          <p className="text-card-foreground">
            确认删除报表「
            <span className="font-medium text-foreground">
              {pendingDelete?.title}
            </span>
            」？该报表将被归档，不再显示在列表中。
          </p>
          {deleteError ? (
            <div className="px-3 py-2 rounded-md bg-danger-light text-danger">
              {deleteError}
            </div>
          ) : null}
        </div>
      </ModalShell>

      <ModalShell
        open={showCreateModal}
        onClose={closeCreateModal}
        title="新建报表"
        footer={
          <>
            <button
              type="button"
              onClick={closeCreateModal}
              disabled={creating}
              className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => void handleCreate()}
              disabled={creating}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {creating ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Plus className="w-4 h-4" />
              )}
              创建
            </button>
          </>
        }
      >
        <div className="space-y-3 text-[13px]">
          <div>
            <label className="block text-[12px] text-muted-foreground mb-1">
              报表标题
            </label>
            <input
              type="text"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="例如：Q3 销售分析"
              autoFocus
              className="w-full px-3 py-2 text-[13px] rounded-md border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent"
            />
          </div>
          <p className="text-[12px] text-muted-foreground">
            报表将以「草稿」状态创建，后续可在列表中查看、编辑或删除。
          </p>
          {createError ? (
            <div className="px-3 py-2 rounded-md bg-danger-light text-danger">
              {createError}
            </div>
          ) : null}
          {createSuccess ? (
            <div className="px-3 py-2 rounded-md bg-success-light text-success inline-flex items-center gap-1.5">
              <CheckCircle2 className="w-4 h-4" />
              {createSuccess}
            </div>
          ) : null}
        </div>
      </ModalShell>

    </div>
  );
}