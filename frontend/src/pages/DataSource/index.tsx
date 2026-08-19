import { useCallback, useState } from "react";
import {
  CheckCircle,
  Database,
  HardDrive,
  RefreshCw,
  Plus,
  Upload,
  Search,
  FileText,
  Table,
  Pencil,
  Trash2,
  Eye,
  ChevronUp,
  X,
  Loader2,
} from "lucide-react";
import { useToast } from "../../components/ui/Toast";
import { useQuery } from "../../hooks/useQuery";
import { pushNotification } from "../../api/notifications";
import {
  listDatasources,
  uploadDatasource,
  previewDatasource,
  updateDatasourceSchema,
  deleteDatasource,
  testConnection,
  connectDatasource,
  syncDatasource,
} from "../../api/datasources";
import type {
  DataSource,
  DataSourcePreview,
  SchemaField,
} from "../../types/datasource";

type SchemaCategory = SchemaField["category"];

const SCHEMA_CATEGORIES: ReadonlyArray<{
  value: SchemaCategory;
  label: string;
}> = [
  { value: "dimension", label: "dimension" },
  { value: "measure", label: "measure" },
  { value: "time", label: "time" },
  { value: "key", label: "string" },
];

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  } catch {
    return "—";
  }
}

function formatRelativeTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  if (diff < 0) return "刚刚";
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return "刚刚";
  if (diff < hour) return `${Math.floor(diff / minute)}分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)}小时前`;
  return `${Math.floor(diff / day)}天前`;
}

function typeBadgeClass(type: DataSource["sourceType"]): {
  label: string;
  bg: string;
  text: string;
} {
  switch (type) {
    case "csv":
      return { label: "CSV", bg: "bg-success-light", text: "text-success" };
    case "excel":
      return { label: "Excel", bg: "bg-ai-light", text: "text-ai" };
    case "mysql":
    case "postgresql":
      return { label: "数据库", bg: "bg-info-light", text: "text-info" };
    default:
      return {
        label: String(type),
        bg: "bg-info-light",
        text: "text-info",
      };
  }
}

function typeIcon(type: DataSource["sourceType"]): {
  Icon: typeof Database;
  bg: string;
  color: string;
} {
  switch (type) {
    case "csv":
      return {
        Icon: FileText,
        bg: "bg-success-light",
        color: "text-success",
      };
    case "excel":
      return { Icon: Table, bg: "bg-ai-light", color: "text-ai" };
    case "mysql":
    case "postgresql":
      return {
        Icon: Database,
        bg: "bg-info-light",
        color: "text-info",
      };
    default:
      return { Icon: Database, bg: "bg-info-light", color: "text-info" };
  }
}

function statusBadge(status: DataSource["status"]): {
  label: string;
  text: string;
  dot: string;
  pulse?: boolean;
} {
  switch (status) {
    case "connected":
      return {
        label: "已连接",
        text: "text-success",
        dot: "bg-success",
      };
    case "syncing":
      return {
        label: "同步中",
        text: "text-info",
        dot: "bg-info",
        pulse: true,
      };
    case "disconnected":
      return {
        label: "断开",
        text: "text-danger",
        dot: "bg-danger",
      };
    default:
      return {
        label: String(status),
        text: "text-muted-foreground",
        dot: "bg-muted-foreground",
      };
  }
}

function describeType(type: DataSource["sourceType"], status: DataSource["status"]): string {
  const statusLabel = statusBadge(status).label;
  if (type === "mysql" || type === "postgresql") {
    return `${type.toUpperCase()} · ${statusLabel}`;
  }
  if (type === "csv") {
    return `CSV 文件 · ${statusLabel}`;
  }
  if (type === "excel") {
    return `Excel 文件 · ${statusLabel}`;
  }
  return statusLabel;
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
      <div className="relative bg-white rounded-lg shadow-xl w-[min(960px,92vw)] max-h-[88vh] flex flex-col">
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

const SYSTEM_TABLES = new Set([
  "ai_messages", "ai_sessions", "alembic_version",
  "users", "datasources", "canvases", "chart_configs",
  "dashboards", "dashboard_charts", "reports",
  "insight_rules", "insight_records", "insight_suggestions",
  "notifications", "operation_logs",
]);

export default function DataSource() {
  const toast = useToast();

  const fetcher = useCallback(
    () => listDatasources({ pageSize: 100 }),
    []
  );
  const {
    data: listData,
    loading: listLoading,
    error: listError,
    refetch,
  } = useQuery(fetcher, []);

  const items = listData?.items ?? [];

  const [typeFilter, setTypeFilter] = useState("全部类型");
  const [statusFilter, setStatusFilter] = useState("全部状态");
  const [searchQuery, setSearchQuery] = useState("");

  const filteredItems = items.filter((item) => {
    // Type filter
    if (typeFilter === "数据库" && !["mysql", "postgresql"].includes(item.sourceType)) return false;
    if (typeFilter === "文件" && !["csv", "excel"].includes(item.sourceType)) return false;
    if (typeFilter === "API") return false; // API type not yet implemented
    // Status filter
    if (statusFilter === "已连接" && item.status !== "connected") return false;
    if (statusFilter === "断开" && item.status !== "disconnected") return false;
    if (statusFilter === "同步中" && item.status !== "syncing") return false;
    // Search
    if (searchQuery && !item.name.toLowerCase().includes(searchQuery.toLowerCase())) return false;
    return true;
  });

  const [uploadState, setUploadState] = useState<{
    state: "idle" | "uploading" | "error";
    message?: string;
  }>({ state: "idle" });

  const [deleteTarget, setDeleteTarget] = useState<DataSource | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const [detailTarget, setDetailTarget] = useState<DataSource | null>(null);
  const [preview, setPreview] = useState<DataSourcePreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [schemaFields, setSchemaFields] = useState<SchemaField[]>([]);
  const [schemaSaving, setSchemaSaving] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // ---- 创建数据源 Modal ----
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createTab, setCreateTab] = useState<"file" | "db">("file");
  const [dbForm, setDbForm] = useState({
    source_type: "mysql" as "mysql" | "postgres",
    host: "localhost",
    port: 3306,
    database: "",
    user: "root",
    password: "",
    table_names: [] as string[],
  });
  const [testResult, setTestResult] = useState<{
    success: boolean;
    error?: string;
    row_count?: number;
    tables?: string[];
  } | null>(null);
  const [testLoading, setTestLoading] = useState(false);
  const [saveLoading, setSaveLoading] = useState(false);

  function openDetail(ds: DataSource) {
    setDetailTarget(ds);
    setPreview(null);
    setPreviewError(null);
    setSchemaError(null);
    setSchemaFields(
      (ds.schemaMeta?.fields ?? []).map((f) => ({ ...f }))
    );
    setPreviewLoading(true);
    previewDatasource(ds.id, 10)
      .then((p) => {
        setPreview(p);
        if (
          schemaFields.length === 0 &&
          ds.schemaMeta?.fields &&
          ds.schemaMeta.fields.length > 0
        ) {
          setSchemaFields(
            ds.schemaMeta.fields.map((f) => ({ ...f }))
          );
        }
      })
      .catch((err) => {
        const msg =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "获取预览失败";
        setPreviewError(msg);
      })
      .finally(() => setPreviewLoading(false));
  }

  function closeDetail() {
    setDetailTarget(null);
    setPreview(null);
    setPreviewError(null);
    setSchemaFields([]);
    setSchemaError(null);
  }

  function updateFieldCategory(index: number, category: SchemaCategory) {
    setSchemaFields((prev) =>
      prev.map((f, i) => (i === index ? { ...f, category } : f))
    );
  }

  async function handleSaveSchema() {
    if (!detailTarget) return;
    setSchemaSaving(true);
    setSchemaError(null);
    try {
      await updateDatasourceSchema(detailTarget.id, {
        schemaMeta: { fields: schemaFields },
      });
      await refetch();
      closeDetail();
    } catch (err) {
      const msg =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : "保存失败";
      setSchemaError(msg);
    } finally {
      setSchemaSaving(false);
    }
  }

  function askDelete(ds: DataSource) {
    setDeleteTarget(ds);
    setDeleteError(null);
  }

  function cancelDelete() {
    setDeleteTarget(null);
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    const targetName = deleteTarget.name;
    try {
      await deleteDatasource(deleteTarget.id);
      setDeleteTarget(null);
      await refetch();
      toast.success(`数据源 "${targetName}" 已删除`);
      pushNotification({
        type: "system",
        title: "数据源已删除",
        body: `"${targetName}" 已成功删除`,
        resourceType: "datasource",
      }).catch(() => {});
    } catch (err) {
      const msg =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : "删除失败";
      setDeleteError(msg);
      toast.error(`删除失败：${msg}`);
    } finally {
      setDeleting(false);
    }
  }

  function openCreateModal() {
    setDbForm({
      source_type: "mysql",
      host: "localhost",
      port: 3306,
      database: "",
      user: "root",
      password: "",
      table_names: [],
    });
    setTestResult(null);
    setTestLoading(false);
    setSaveLoading(false);
    setCreateTab("file");
    setShowCreateModal(true);
  }

  function handleCreateFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setShowCreateModal(false);
    setUploadState({ state: "uploading" });
    const nameFromFile = file.name.replace(/\.[^.]+$/, "");
    uploadDatasource(file, nameFromFile)
      .then((ds) => {
        setUploadState({ state: "idle" });
        refetch();
        pushNotification({ type: "system", title: "数据源就绪", body: `"${nameFromFile}" 已上传成功，可以开始分析了`, resourceType: "datasource" }).catch(() => {});
      })
      .catch((err) => {
        const msg =
          err instanceof Error
            ? err.message
            : typeof err === "string"
              ? err
              : "上传失败";
        setUploadState({ state: "error", message: msg });
      });
  }

  function updateDbField<K extends keyof typeof dbForm>(
    key: K,
    value: (typeof dbForm)[K]
  ) {
    setDbForm((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "source_type") {
        if (value === "postgres") {
          next.port = 5432;
          next.user = next.user === "root" ? "postgres" : next.user;
        } else {
          next.port = 3306;
          next.user = next.user === "postgres" ? "root" : next.user;
        }
      }
      return next;
    });
  }

  const handleTest = async () => {
    setTestLoading(true);
    setTestResult(null);
    try {
      const res = await testConnection({
        source_type: dbForm.source_type === "postgres" ? "postgresql" : "mysql",
        connection_info: {
          host: dbForm.host,
          port: dbForm.port,
          user: dbForm.user,
          password: dbForm.password,
          database: dbForm.database,
        },
      });
      if (res.success) {
        setTestResult(res);
        // Auto-select all non-system tables
        if (res.tables && res.tables.length > 0) {
          const preferred = res.tables!.filter((t: string) => !SYSTEM_TABLES.has(t) && !t.startsWith("_"));
          setDbForm((prev) => ({ ...prev, table_names: preferred }));
        }
      } else {
        // Map backend error to clean Chinese message, but keep original for debugging
        const errMsg = (res.error || "").toString();
        let friendly = "连接失败";
        if (/password authentication failed|password.*incorrect/i.test(errMsg)) {
          friendly = "密码错误";
        } else if (/could not connect|connection refused|no route to host/i.test(errMsg)) {
          friendly = "无法连接到数据库，请检查主机和端口";
        } else if (/database.*not exist|does not exist/i.test(errMsg)) {
          friendly = "数据库不存在";
        } else if (/authentication failed|login failed/i.test(errMsg)) {
          friendly = "认证失败，请检查用户名和密码";
        }
        // 附带原始错误信息，方便排查
        const displayError = errMsg ? `${friendly}（${errMsg}）` : friendly;
        setTestResult({ success: false, error: displayError });
        toast.error(displayError);
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      const msg = typeof detail === "string" ? detail : (detail?.message || e?.message || "请求失败");
      setTestResult({ success: false, error: msg });
      toast.error(`请求失败：${msg}`);
    } finally {
      setTestLoading(false);
    }
  };

  const handleSave = async () => {
    if (!testResult?.success) return;
    const selected = dbForm.table_names;
    if (selected.length === 0) {
      toast.warning("请至少选择一张表");
      return;
    }
    setSaveLoading(true);
    try {
      for (let i = 0; i < selected.length; i++) {
        const tname = selected[i];
        const name = tname.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
        const ds = await connectDatasource({
          name,
          sourceType: dbForm.source_type === "postgres" ? "postgresql" : "mysql",
          host: dbForm.host,
          port: dbForm.port,
          dbName: dbForm.database,
          username: dbForm.user,
          password: dbForm.password,
          tableName: tname,
        });
        // Auto-sync after creation
        await syncDatasource(ds.id);
        if (i === selected.length - 1) {
          // Last one - close modal
          setShowCreateModal(false);
          refetch();
          const label = selected.length > 1 ? `${selected.length} 个数据源` : (selected[0] || "数据源");
          pushNotification({ type: "system", title: "数据源就绪", body: `${label}已连接成功，可以开始分析了`, resourceType: "datasource" }).catch(() => {});
        }
      }
    } catch (err) {
      const msg =
        err instanceof Error
          ? err.message
          : typeof err === "string"
            ? err
            : "创建数据源失败";
      toast.error(msg);
    } finally {
      setSaveLoading(false);
    }
  };

  const totalRowCount = items.reduce(
    (sum, ds) => sum + (ds.rowCount || 0),
    0
  );
  const totalBytes = items.reduce(
    (sum, ds) => sum + (ds.sizeBytes || 0),
    0
  );

  return (
    <div className="flex-1 p-6 space-y-5">
      <header className="flex items-center justify-between">
        <h1 className="text-[17px] font-semibold text-foreground">
          源数据管理
        </h1>
        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={openCreateModal}
            className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm"
          >
            <Plus className="w-4 h-4" />
            新建数据源
          </button>
        </div>
      </header>

      {uploadState.state === "error" ? (
        <div className="bg-danger-light border border-danger/20 text-danger text-[13px] rounded-md px-4 py-2.5">
          上传失败：{uploadState.message ?? "未知错误"}
        </div>
      ) : null}

      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white rounded-md shadow-card border-l-[3px] border-primary p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-primary-light flex items-center justify-center shrink-0">
            <Database className="w-5 h-5 text-primary" />
          </div>
          <div>
            <p className="text-[12px] text-muted-foreground mb-0.5">
              数据源总数
            </p>
            <p className="text-[22px] font-bold text-foreground leading-none">
              {listLoading && items.length === 0 ? "—" : items.length}
            </p>
          </div>
        </div>

        <div className="bg-white rounded-md shadow-card border-l-[3px] border-primary p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-primary-light flex items-center justify-center shrink-0">
            <HardDrive className="w-5 h-5 text-primary" />
          </div>
          <div>
            <p className="text-[12px] text-muted-foreground mb-0.5">
              数据总量
            </p>
            <p className="text-[22px] font-bold text-foreground leading-none">
              {listLoading && items.length === 0
                ? "—"
                : formatBytes(totalBytes)}
            </p>
          </div>
        </div>

        <div className="bg-white rounded-md shadow-card border-l-[3px] border-primary p-5 flex items-center gap-4">
          <div className="w-10 h-10 rounded-lg bg-primary-light flex items-center justify-center shrink-0">
            <RefreshCw className="w-5 h-5 text-primary" />
          </div>
          <div>
            <p className="text-[12px] text-muted-foreground mb-0.5">
              总行数
            </p>
            <p className="text-[22px] font-bold text-foreground leading-none">
              {listLoading && items.length === 0
                ? "—"
                : totalRowCount.toLocaleString()}
              <span className="text-[14px] font-medium text-muted-foreground">
                {" "}
                行
              </span>
            </p>
          </div>
        </div>
      </div>

      <div className="bg-white rounded-md shadow-card">
        <div className="px-5 py-4 border-b border-border-light flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="relative">
              <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                placeholder="搜索数据源..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="pl-9 pr-3 py-2 text-[13px] rounded-lg border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent w-[220px] transition-shadow"
              />
            </div>
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
            >
              <option>全部类型</option>
              <option>数据库</option>
              <option>文件</option>
              <option>API</option>
            </select>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="px-3 py-2 text-[13px] rounded-lg border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
            >
              <option>全部状态</option>
              <option>已连接</option>
              <option>断开</option>
              <option>同步中</option>
            </select>
          </div>
          <span className="text-[12px] text-muted-foreground">
            {listLoading && items.length === 0
              ? "加载中..."
              : listError
                ? "加载失败"
                : typeFilter !== "全部类型" || statusFilter !== "全部状态" || searchQuery
                  ? `已筛选 ${filteredItems.length} / ${items.length} 个数据源`
                  : `共 ${items.length} 个数据源`}
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border-light">
                <th className="text-left px-5 py-3 font-medium text-muted-foreground text-[12px] uppercase tracking-wide">
                  名称
                </th>
                <th className="text-left px-5 py-3 font-medium text-muted-foreground text-[12px] uppercase tracking-wide">
                  类型
                </th>
                <th className="text-left px-5 py-3 font-medium text-muted-foreground text-[12px] uppercase tracking-wide">
                  状态
                </th>
                <th className="text-left px-5 py-3 font-medium text-muted-foreground text-[12px] uppercase tracking-wide">
                  数据量
                </th>
                <th className="text-left px-5 py-3 font-medium text-muted-foreground text-[12px] uppercase tracking-wide">
                  最后同步
                </th>
                <th className="text-right px-5 py-3 font-medium text-muted-foreground text-[12px] uppercase tracking-wide">
                  操作
                </th>
              </tr>
            </thead>
            <tbody>
              {listLoading && items.length === 0 ? (
                <tr>
                  <td
                    colSpan={6}
                    className="px-5 py-10 text-center text-[13px] text-muted-foreground"
                  >
                    <span className="inline-flex items-center gap-2">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      正在加载数据源...
                    </span>
                  </td>
                </tr>
              ) : listError ? (
                <tr>
                  <td
                    colSpan={6}
                    className="px-5 py-10 text-center text-[13px] text-danger"
                  >
                    加载失败：{listError.message}
                  </td>
                </tr>
              ) : filteredItems.length === 0 ? (
                <tr>
                  <td
                    colSpan={6}
                    className="px-5 py-10 text-center text-[13px] text-muted-foreground"
                  >
                    {searchQuery || typeFilter !== "全部类型" || statusFilter !== "全部状态"
                      ? "没有符合条件的数据源"
                      : '暂无数据源，点击右上角"新建数据源"开始添加'}
                  </td>
                </tr>
              ) : (
                filteredItems.map((ds) => {
                  const badge = typeBadgeClass(ds.sourceType);
                  const statusInfo = statusBadge(ds.status);
                  const iconInfo = typeIcon(ds.sourceType);
                  const Icon = iconInfo.Icon;
                  return (
                    <tr
                      key={ds.id}
                      onClick={() => openDetail(ds)}
                      className="border-b border-border-light hover:bg-muted transition-colors cursor-pointer"
                    >
                      <td className="px-5 py-3.5">
                        <div className="flex items-center gap-3">
                          <div
                            className={`w-8 h-8 rounded-lg ${iconInfo.bg} flex items-center justify-center shrink-0`}
                          >
                            <Icon className={`w-4 h-4 ${iconInfo.color}`} />
                          </div>
                          <div>
                            <p className="font-medium text-foreground">
                              {ds.name}
                            </p>
                            <p className="text-[12px] text-muted-foreground mt-0.5">
                              {describeType(ds.sourceType, ds.status)}
                            </p>
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-3.5">
                        <span
                          className={`inline-flex px-2 py-0.5 rounded text-[12px] font-medium ${badge.bg} ${badge.text}`}
                        >
                          {badge.label}
                        </span>
                      </td>
                      <td className="px-5 py-3.5">
                        <span
                          className={`inline-flex items-center gap-1.5 text-[12px] font-medium ${statusInfo.text}`}
                        >
                          <span
                            className={`w-1.5 h-1.5 rounded-full ${statusInfo.dot} ${
                              statusInfo.pulse ? "animate-pulse" : ""
                            }`}
                          />
                          {statusInfo.label}
                        </span>
                      </td>
                      <td className="px-5 py-3.5 text-card-foreground">
                        {(ds.rowCount || 0).toLocaleString()} 行 ·{" "}
                        {formatBytes(ds.sizeBytes || 0)}
                      </td>
                      <td className="px-5 py-3.5 text-muted-foreground">
                        {formatRelativeTime(ds.lastSyncedAt)}
                      </td>
                      <td className="px-5 py-3.5 text-right">
                        <div
                          className="flex items-center justify-end gap-1"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <button
                            type="button"
                            onClick={() => openDetail(ds)}
                            className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                            title="查看/编辑"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          {(ds.sourceType === "postgresql" || ds.sourceType === "mysql") && (
                            <button
                              type="button"
                              onClick={() => {
                                syncDatasource(ds.id).then(() => refetch()).catch((e: Error) => alert(`同步失败: ${e.message}`));
                              }}
                              className="p-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-primary transition-colors"
                              title="重新连接并同步"
                            >
                              <RefreshCw className="w-3.5 h-3.5" />
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => askDelete(ds)}
                            className="p-1.5 rounded-md hover:bg-danger-light text-muted-foreground hover:text-danger transition-colors"
                            title="删除"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {items.length > 0 && items[0] ? (
        <div className="bg-white rounded-md shadow-card">
          <div className="px-5 py-4 border-b border-border-light flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Eye className="w-4 h-4 text-primary" />
              <h3 className="text-[14px] font-semibold text-foreground">
                数据预览 — {items[0].name}
              </h3>
              <span className="text-[12px] text-muted-foreground ml-1">
                最近 5 条记录
              </span>
            </div>
            <button
              className="p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
              title="折叠"
            >
              <ChevronUp className="w-4 h-4" />
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-[13px] min-w-[600px]">
              <thead>
                <tr className="bg-muted">
                  <th className="text-left px-5 py-2.5 font-medium text-muted-foreground text-[12px]">
                    字段 1
                  </th>
                  <th className="text-left px-5 py-2.5 font-medium text-muted-foreground text-[12px]">
                    字段 2
                  </th>
                  <th className="text-left px-5 py-2.5 font-medium text-muted-foreground text-[12px]">
                    字段 3
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td
                    colSpan={3}
                    className="px-5 py-6 text-center text-[13px] text-muted-foreground"
                  >
                    在表格中点击数据源查看前 10 行预览
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="bg-white rounded-md shadow-card">
          <div className="px-5 py-4 border-b border-border-light flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Eye className="w-4 h-4 text-primary" />
              <h3 className="text-[14px] font-semibold text-foreground">
                数据预览
              </h3>
              <span className="text-[12px] text-muted-foreground ml-1">
                暂无数据
              </span>
            </div>
            <button
              className="p-1.5 rounded-md hover:bg-muted text-muted-foreground transition-colors"
              title="折叠"
            >
              <ChevronUp className="w-4 h-4" />
            </button>
          </div>
          <div className="px-5 py-8 text-center text-[13px] text-muted-foreground">
            请先上传或连接一个数据源，再查看数据预览
          </div>
        </div>
      )}

      <ModalShell
        open={detailTarget !== null}
        onClose={closeDetail}
        title={detailTarget ? `数据源详情 — ${detailTarget.name}` : ""}
        footer={
          <>
            <button
              type="button"
              onClick={closeDetail}
              disabled={schemaSaving}
              className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSaveSchema}
              disabled={schemaSaving}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
            >
              {schemaSaving ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : null}
              保存
            </button>
          </>
        }
      >
        {detailTarget ? (
          <div className="space-y-5">

            <section>
              <h4 className="text-[13px] font-semibold text-foreground mb-2">
                基本信息
              </h4>
              <div className="grid grid-cols-2 gap-3 text-[13px]">
                <div>
                  <p className="text-muted-foreground text-[12px] mb-0.5">
                    名称
                  </p>
                  <p className="text-card-foreground">{detailTarget.name}</p>
                </div>
                <div>
                  <p className="text-muted-foreground text-[12px] mb-0.5">
                    类型
                  </p>
                  <p className="text-card-foreground">
                    {detailTarget.sourceType}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground text-[12px] mb-0.5">
                    状态
                  </p>
                  <p className="text-card-foreground">
                    {statusBadge(detailTarget.status).label}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground text-[12px] mb-0.5">
                    行数
                  </p>
                  <p className="text-card-foreground">
                    {(detailTarget.rowCount || 0).toLocaleString()}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground text-[12px] mb-0.5">
                    大小
                  </p>
                  <p className="text-card-foreground">
                    {formatBytes(detailTarget.sizeBytes || 0)}
                  </p>
                </div>
                <div>
                  <p className="text-muted-foreground text-[12px] mb-0.5">
                    创建时间
                  </p>
                  <p className="text-card-foreground">
                    {formatDateTime(detailTarget.createdAt)}
                  </p>
                </div>
              </div>
            </section>

            <section>
              <h4 className="text-[13px] font-semibold text-foreground mb-2">
                数据预览（前 10 行）
              </h4>
              {previewLoading ? (
                <div className="px-3 py-6 text-center text-[13px] text-muted-foreground">
                  <span className="inline-flex items-center gap-2">
                    <Loader2 className="w-4 h-4 animate-spin" />
                    正在加载预览...
                  </span>
                </div>
              ) : previewError ? (
                <div className="px-3 py-4 rounded-md bg-danger-light text-danger text-[13px]">
                  {previewError}
                </div>
              ) : preview && preview.columns.length > 0 ? (
                <div className="overflow-x-auto border border-border-light rounded-md">
                  <table className="w-full text-[13px] min-w-[600px]">
                    <thead>
                      <tr className="bg-muted">
                        {preview.columns.map((col) => (
                          <th
                            key={col}
                            className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px] whitespace-nowrap"
                          >
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {preview.rows.slice(0, 10).map((row, ri) => (
                        <tr
                          key={ri}
                          className="border-t border-border-light"
                        >
                          {preview.columns.map((_, ci) => {
                            const cell = row[ci];
                            const display =
                              cell === null || cell === undefined
                                ? "—"
                                : typeof cell === "object"
                                  ? JSON.stringify(cell)
                                  : String(cell);
                            return (
                              <td
                                key={ci}
                                className="px-3 py-2 text-card-foreground whitespace-nowrap"
                              >
                                {display}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="px-3 py-6 text-center text-[13px] text-muted-foreground">
                  暂无预览数据
                </div>
              )}
            </section>

            <section>
              <h4 className="text-[13px] font-semibold text-foreground mb-2">
                字段元数据
              </h4>
              {schemaFields.length === 0 ? (
                <div className="px-3 py-6 text-center text-[13px] text-muted-foreground">
                  该数据源暂未生成字段元数据
                </div>
              ) : (
                <div className="overflow-x-auto border border-border-light rounded-md">
                  <table className="w-full text-[13px] min-w-[600px]">
                    <thead>
                      <tr className="bg-muted">
                        <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">
                          字段名
                        </th>
                        <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">
                          显示名
                        </th>
                        <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">
                          数据类型
                        </th>
                        <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">
                          类型
                        </th>
                        <th className="text-left px-3 py-2 font-medium text-muted-foreground text-[12px]">
                          可空
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {schemaFields.map((field, idx) => (
                        <tr
                          key={`${field.name}-${idx}`}
                          className="border-t border-border-light"
                        >
                          <td className="px-3 py-2 font-mono text-[12px] text-foreground">
                            {field.name}
                          </td>
                          <td className="px-3 py-2 text-card-foreground">
                            {field.displayName || field.name}
                          </td>
                          <td className="px-3 py-2 text-card-foreground">
                            {field.dataType}
                          </td>
                          <td className="px-3 py-2">
                            <select
                              value={field.category}
                              onChange={(e) =>
                                updateFieldCategory(
                                  idx,
                                  e.target.value as SchemaCategory
                                )
                              }
                              className="px-2 py-1 text-[12px] rounded border border-border bg-white text-card-foreground focus:outline-none focus:ring-2 focus:ring-ring cursor-pointer"
                            >
                              {SCHEMA_CATEGORIES.map((opt) => (
                                <option
                                  key={opt.value}
                                  value={opt.value}
                                >
                                  {opt.label}
                                </option>
                              ))}
                            </select>
                          </td>
                          <td className="px-3 py-2 text-card-foreground">
                            {field.nullable ? "是" : "否"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {schemaError ? (
                <div className="mt-2 px-3 py-2 rounded-md bg-danger-light text-danger text-[13px]">
                  {schemaError}
                </div>
              ) : null}
            </section>
          </div>
        ) : null}
      </ModalShell>

      <ModalShell
        open={deleteTarget !== null}
        onClose={cancelDelete}
        title="确认删除"
        footer={
          <>
            <button
              type="button"
              onClick={cancelDelete}
              disabled={deleting}
              className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
            >
              取消
            </button>
            <button
              type="button"
              onClick={confirmDelete}
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
        {deleteTarget ? (
          <div className="space-y-3 text-[13px] text-card-foreground">
            <p>
              确定要删除数据源
              <span className="font-semibold text-foreground mx-1">
                {deleteTarget.name}
              </span>
              吗？该操作无法撤销。
            </p>
            {deleteError ? (
              <div className="px-3 py-2 rounded-md bg-danger-light text-danger text-[13px]">
                {deleteError}
              </div>
            ) : null}
          </div>
        ) : null}
      </ModalShell>

      {/* 创建数据源 Modal */}
      <ModalShell
        open={showCreateModal}
        onClose={() => {
          setShowCreateModal(false);
          setTestResult(null);
        }}
        title="新建数据源"
        footer={
          createTab === "db" ? (
            <>
              <button
                type="button"
                onClick={() => {
                  setShowCreateModal(false);
                  setTestResult(null);
                }}
                className="inline-flex items-center px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleTest}
                disabled={testLoading || !dbForm.host || !dbForm.database || !dbForm.user}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-card-foreground bg-white border border-border hover:bg-muted transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {testLoading ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : null}
                测试连接
              </button>
              <button
                type="button"
                onClick={handleSave}
                disabled={!testResult?.success || saveLoading}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {saveLoading ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : null}
                保存
              </button>
            </>
          ) : null
        }
      >
        {/* Tab switcher */}
        <div className="flex border-b border-border-light mb-4">
          <button
            type="button"
            onClick={() => {
              setCreateTab("file");
              setTestResult(null);
            }}
            className={`px-4 py-2.5 text-[13px] font-medium border-b-2 transition-colors ${
              createTab === "file"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            文件上传
          </button>
          <button
            type="button"
            onClick={() => {
              setCreateTab("db");
              setTestResult(null);
            }}
            className={`px-4 py-2.5 text-[13px] font-medium border-b-2 transition-colors ${
              createTab === "db"
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground"
            }`}
          >
            数据库直连
          </button>
        </div>

        {/* 文件上传 Tab */}
        {createTab === "file" ? (
          <div className="space-y-4">
            <p className="text-[13px] text-muted-foreground">
              支持 CSV 和 Excel (.xlsx, .xls) 格式文件上传
            </p>
            <div className="border-2 border-dashed border-border rounded-lg p-8 text-center hover:border-primary/40 transition-colors">
              <Upload className="w-8 h-8 text-muted-foreground mx-auto mb-3" />
              <p className="text-[13px] text-muted-foreground mb-2">
                将文件拖拽到此处，或点击下方按钮选择文件
              </p>
              <label className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-[13px] font-medium text-white bg-primary hover:bg-primary-hover transition-colors shadow-sm cursor-pointer">
                <Upload className="w-4 h-4" />
                选择文件
                <input
                  type="file"
                  accept=".csv,.xlsx,.xls"
                  className="hidden"
                  onChange={handleCreateFileChange}
                />
              </label>
            </div>
          </div>
        ) : (
          /* 数据库直连 Tab */
          <div className="space-y-4">
            {/* 数据库类型 */}
            <div>
              <label className="block text-[13px] font-medium text-foreground mb-1.5">
                数据库类型
              </label>
              <div className="flex items-center gap-4">
                <label className="inline-flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    name="dbType"
                    value="mysql"
                    checked={dbForm.source_type === "mysql"}
                    onChange={() => updateDbField("source_type", "mysql")}
                    className="w-3.5 h-3.5 text-primary"
                  />
                  <span className="text-[13px] text-card-foreground">MySQL</span>
                </label>
                <label className="inline-flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    name="dbType"
                    value="postgres"
                    checked={dbForm.source_type === "postgres"}
                    onChange={() => updateDbField("source_type", "postgres")}
                    className="w-3.5 h-3.5 text-primary"
                  />
                  <span className="text-[13px] text-card-foreground">PostgreSQL</span>
                </label>
              </div>
            </div>

            {/* 主机 + 端口 */}
            <div className="grid grid-cols-3 gap-3">
              <div className="col-span-2">
                <label className="block text-[13px] font-medium text-foreground mb-1.5">
                  主机
                </label>
                <input
                  type="text"
                  value={dbForm.host}
                  onChange={(e) => updateDbField("host", e.target.value)}
                  placeholder="localhost"
                  className="w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-shadow"
                />
              </div>
              <div>
                <label className="block text-[13px] font-medium text-foreground mb-1.5">
                  端口
                </label>
                <input
                  type="number"
                  value={dbForm.port}
                  onChange={(e) =>
                    updateDbField("port", Number(e.target.value) || 0)
                  }
                  className="w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-shadow"
                />
              </div>
            </div>

            {/* 数据库名 */}
            <div>
              <label className="block text-[13px] font-medium text-foreground mb-1.5">
                数据库名
              </label>
              <input
                type="text"
                value={dbForm.database}
                onChange={(e) => updateDbField("database", e.target.value)}
                placeholder="请输入数据库名"
                className="w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-shadow"
              />
            </div>

            {/* 用户名 + 密码 */}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-[13px] font-medium text-foreground mb-1.5">
                  用户名
                </label>
                <input
                  type="text"
                  value={dbForm.user}
                  onChange={(e) => updateDbField("user", e.target.value)}
                  placeholder="root"
                  className="w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-shadow"
                />
              </div>
              <div>
                <label className="block text-[13px] font-medium text-foreground mb-1.5">
                  密码
                </label>
                <input
                  type="password"
                  value={dbForm.password}
                  onChange={(e) => updateDbField("password", e.target.value)}
                  placeholder="请输入密码"
                  className="w-full px-3 py-2 text-[13px] rounded-lg border border-border bg-input placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring focus:border-transparent transition-shadow"
                />
              </div>
            </div>

            {/* 选择表 - 连接成功后批量勾选 */}
            {testResult?.success && testResult.tables && testResult.tables.length > 0 ? (() => {
              const visibleTables = testResult.tables.filter((t: string) => !SYSTEM_TABLES.has(t));
              if (visibleTables.length === 0) return null;
              return (
              <div>
                <label className="block text-[13px] font-medium text-foreground mb-1.5">
                  选择表（已选 {dbForm.table_names.length} 张）
                </label>
                <div className="max-h-[180px] overflow-y-auto border border-border rounded-lg p-2 space-y-1">
                  {visibleTables.map((t: string) => (
                      <label
                        key={t}
                        className="flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] cursor-pointer hover:bg-accent/50"
                      >
                        <input
                          type="checkbox"
                          checked={dbForm.table_names.includes(t)}
                          onChange={() => {
                            setDbForm((prev) => ({
                              ...prev,
                              table_names: prev.table_names.includes(t)
                                ? prev.table_names.filter((n) => n !== t)
                                : [...prev.table_names, t],
                            }));
                          }}
                          className="w-3.5 h-3.5 text-primary rounded"
                        />
                        <span className="text-muted-foreground text-[12px] font-mono">{t}</span>
                      </label>
                  ))}
                </div>
              </div>
              );
            })() : null}

            {/* Test result */}
            {testResult ? (
              <div
                className={`px-4 py-3 rounded-md text-[13px] flex items-center gap-2 ${
                  testResult.success
                    ? "bg-green-50 text-green-700 border border-green-200"
                    : "bg-red-50 text-red-700 border border-red-200"
                }`}
              >
                {testResult.success ? (
                  <>
                    <CheckCircle className="w-4 h-4 text-green-600 shrink-0" />
                    <span>
                      连接成功
                      {testResult.row_count != null
                        ? `，发现 ${testResult.row_count} 张表`
                        : ""}
                    </span>
                  </>
                ) : (
                  <>
                    <AlertTriangle className="w-4 h-4 text-red-600 shrink-0" />
                    <span>{testResult.error || "连接失败"}</span>
                  </>
                )}
              </div>
            ) : null}
          </div>
        )}
      </ModalShell>
    </div>
  );
}
