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
  title: string | null;
  createdAt: string;
}

export interface AISessionDetail extends AISession {
  messages: AIMessage[];
}

export interface AIMessage {
  id: string;
  sessionId: string;
  role: AIMessageRole;
  content: string;
  chartData: Record<string, unknown> | null;
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
