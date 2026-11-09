import type { ChartQueryConfig } from "./chart";

export interface ApiSuccess<T> {
  success: true;
  data: T;
}

export interface ApiError {
  success: false;
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

export type ApiResponse<T> = ApiSuccess<T> | ApiError;

export interface PaginatedResult<T> {
  items: T[];
  total: number;
  page: number;
  pageSize: number;
  pages: number;
}

export interface AuthTokens {
  accessToken: string;
  refreshToken: string;
  tokenType: string;
  expiresIn: number;
}

export interface PaginationParams {
  page?: number;
  pageSize?: number;
  search?: string;
}

export type AIMessageRole = "user" | "assistant";

export interface AISession {
  id: string;
  userId: string;
  model: string;
  entry: string;          // "chat" | "canvas"
  canvasId: string | null;
  title: string | null;
  createdAt: string;
  /** Redis 任务态标记：true 表示该会话上次任务中断未完成（画布会话列表返回） */
  interrupted?: boolean;
  /** 进程内后台任务是否在跑：对话入口返回，前端据此自动续收（刷新后恢复进行中的回复） */
  running?: boolean;
}

export interface AISessionDetail extends AISession {
  messages: AIMessage[];
}

/** 本轮过程步骤投影（assistant 消息才有；后端从 agent_calls 事实源生成） */
export interface MessageStep {
  id: string;
  kind: "tool" | "llm" | "dispatch";
  toolName: string;
  ok: boolean;
  durationMs: number;
  summary: string;
  args?: Record<string, unknown> | null;
  result?: string | null;
  tokens?: number;
  model?: string;
  errorKind?: string;
  errorMsg?: string;
}

export interface AIMessage {
  id: string;
  sessionId: string;
  role: AIMessageRole;
  content: string;
  chartData: Record<string, unknown> | null;
  steps?: MessageStep[] | null;
  createdAt: string;
}

export interface AICleanRequest {
  datasourceId: string;
  rules?: Record<string, unknown>;
}

export interface AICleanSuggestion {
  field: string;
  action: string;
  rationale: string;
}

export interface AICleanResult {
  suggestions: AICleanSuggestion[];
}

export interface AIRecommendRequest {
  datasourceId: string;
  context?: string;
}

export interface AIRecommendChart {
  title: string;
  config: ChartQueryConfig;
  rationale: string;
}

export interface AIRecommendResult {
  suggestions: AIRecommendChart[];
}
