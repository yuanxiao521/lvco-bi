import apiClient, { extractData, unwrapApi } from "./client";

export interface OperationLogItem {
  id: string;
  userId: string | null;
  userEmail: string | null;
  userDisplayName: string | null;
  action: string;
  resourceType: string;
  resourceId: string | null;
  method: string;
  path: string;
  statusCode: number;
  durationMs: number;
  ipAddress: string | null;
  userAgent: string | null;
  createdAt: string;
}

export interface OperationLogResult {
  items: OperationLogItem[];
  total: number;
  page: number;
  pageSize: number;
  pages: number;
}

export interface OperationLogParams {
  page?: number;
  pageSize?: number;
  userId?: string;
  action?: string;
  resourceType?: string;
  statusCode?: number;
  method?: string;
  search?: string;
  startAt?: string;
  endAt?: string;
}

export async function listOperationLogs(
  params: OperationLogParams = {},
): Promise<OperationLogResult> {
  const response = await apiClient.get("/audit/logs", { params });
  return unwrapApi<OperationLogResult>(extractData(response));
}

export interface AuditSummary {
  total24h: number;
  error24h: number;
  byAction: Array<{ action: string; count: number }>;
  byResource: Array<{ resourceType: string; count: number }>;
  since: string;
}

export async function getAuditSummary(): Promise<AuditSummary> {
  const response = await apiClient.get("/audit/logs/summary");
  return unwrapApi<AuditSummary>(response.data);
}

/** 用当前筛选条件导出 CSV（浏览器直接触发下载） */
export async function exportLogsCsv(params: OperationLogParams = {}): Promise<void> {
  const baseURL =
    apiClient.defaults.baseURL || "http://127.0.0.1:8000/api/v1";
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
  });
  const token = localStorage.getItem("access_token");
  const resp = await fetch(`${baseURL}/audit/logs/export?${qs.toString()}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) throw new Error(`导出失败: ${resp.status}`);
  const blob = await resp.blob();
  const dispo = resp.headers.get("content-disposition") || "";
  const m = dispo.match(/filename="?([^";]+)"?/);
  const filename = m?.[1] || `audit_logs_${Date.now()}.csv`;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export const RESOURCE_TYPE_LABELS: Record<string, string> = {
  auth: "认证",
  user: "用户",
  canvas: "画布",
  datasource: "数据源",
  dashboard: "仪表盘",
  dashboard_chart: "看板图表",
  report: "报表",
  ai: "AI 助手",
  statistics: "统计",
  trash: "回收站",
  insight: "智能洞察",
  notification: "通知",
  permission: "权限",
  audit: "审计",
  share: "分享",
  other: "其他",
};

export const ACTION_LABELS: Record<string, string> = {
  "auth.login": "登录",
  "auth.logout": "登出",
  "auth.register": "注册",
  "auth.refresh": "刷新令牌",
  "canvas.create": "新建画布",
  "canvas.update": "更新画布",
  "canvas.delete": "删除画布",
  "canvas.read": "查看画布",
  "canvas.list": "查看画布列表",
  "canvas.restore": "恢复画布",
  "datasource.create": "新建数据源",
  "datasource.update": "更新数据源",
  "datasource.delete": "删除数据源",
  "dashboard.create": "新建仪表盘",
  "dashboard.update": "更新仪表盘",
  "dashboard.delete": "删除仪表盘",
  "report.create": "新建报表",
  "report.update": "更新报表",
  "report.delete": "删除报表",
  "ai.query": "AI 查询",
  "ai.recommend": "AI 推荐",
  "ai.clean": "AI 清洗",
  "ai.insights": "AI 洞察",
  "user.update": "更新用户角色",
};

export function actionLabel(action: string): string {
  if (ACTION_LABELS[action]) return ACTION_LABELS[action];
  // 拆解 resource.verb
  const [resource, verb] = action.split(".");
  if (!verb) return action;
  const resourceLabel = RESOURCE_TYPE_LABELS[resource] ?? resource;
  const verbMap: Record<string, string> = {
    create: "新建",
    update: "更新",
    delete: "删除",
    read: "查看",
    list: "查看列表",
    query: "查询",
    recommend: "推荐",
    clean: "清洗",
    insights: "洞察",
    login: "登录",
    logout: "登出",
    register: "注册",
    refresh: "刷新",
    restore: "恢复",
  };
  return `${verbMap[verb] ?? verb} ${resourceLabel}`;
}

export function statusColor(code: number): string {
  if (code >= 500) return "bg-danger-light text-danger";
  if (code >= 400) return "bg-warning-light text-warning";
  if (code >= 300) return "bg-info-light text-info";
  if (code >= 200) return "bg-success-light text-success";
  return "bg-muted text-muted-foreground";
}
