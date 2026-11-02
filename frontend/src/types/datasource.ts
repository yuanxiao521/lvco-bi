import type { PaginationParams } from "./api";

export type DatasourceType = "csv" | "excel" | "mysql" | "postgresql";
export type DatasourceStatus = "connected" | "disconnected" | "syncing";

/** 连接级 TLS 模式（对齐 libpq sslmode） */
export type SslMode = "disable" | "prefer" | "require" | "verify-ca" | "verify-full";

export interface SchemaField {
  name: string;
  dataType: string;
  nullable: boolean;
  category: "key" | "measure" | "dimension" | "time";
  displayName: string;
  sample?: unknown[];
  /** 敏感列：不出样例、预览打码、不可用于查询与图表（可在详情里手工开关） */
  sensitive?: boolean;
  sensitiveReason?: string;
}

export interface DataSource {
  id: string;
  userId: string;
  name: string;
  sourceType: DatasourceType;
  /** 数据库型数据源所属连接（一份凭据 → 多张表） */
  connectionId?: string | null;
  hasPassword?: boolean;
  connectionConfig: Record<string, unknown> | null;
  filePath: string | null;
  schemaMeta: { fields: SchemaField[] } | null;
  status: DatasourceStatus;
  sizeBytes: number;
  rowCount: number;
  lastSyncedAt: string | null;
  createdAt: string;
  updatedAt: string | null;
}

/** 数据库连接（凭据、TLS、健康状态都在连接上） */
export interface ConnectionInfo {
  id: string;
  name: string;
  sourceType: "postgresql" | "mysql";
  host: string;
  port: number;
  dbName: string;
  username: string;
  hasPassword: boolean;
  /** manual = 前端输入后加密落库；reference = 部署侧注入的凭据引用（密码不落库、不经浏览器） */
  credentialSource: "manual" | "reference";
  credentialRef: string | null;
  sslMode: SslMode;
  sslCaConfigured: boolean;
  status: DatasourceStatus;
  healthStatus: "ok" | "failed" | null;
  healthError: string | null;
  lastHealthAt: string | null;
  datasourceCount: number;
  createdAt: string;
  updatedAt: string | null;
}

export interface ConnectDatasourceResponse {
  connection: ConnectionInfo;
  datasources: DataSource[];
}

export interface ConnectionHealthResult {
  status: "ok" | "failed" | null;
  error: string | null;
  checkedAt: string | null;
}

export interface DataSourcePreview {
  datasourceId: string;
  columns: string[];
  rows: unknown[][];
  totalRows: number;
  previewRows: number;
}

export interface DataSourceCreatePayload {
  name: string;
  sourceType: DatasourceType;
  connectionConfig?: Record<string, unknown> | null;
  filePath?: string | null;
}

export interface DataSourceUpdatePayload {
  name?: string;
  status?: DatasourceStatus;
  schemaMeta?: { fields: SchemaField[] } | null;
}

export interface DatasourceListParams extends PaginationParams {
  type?: DatasourceType;
  status?: DatasourceStatus;
}
