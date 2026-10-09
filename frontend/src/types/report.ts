import type { PaginationParams } from "./api";
import type { CanvasBlock } from "./canvas";

export type ReportStatus = "draft" | "published" | "shared";
export type ReportSourceType = "canvas" | "dashboard" | "manual" | "ai_insight";

export interface Report {
  id: string;
  userId: string;
  title: string;
  sourceType: ReportSourceType;
  sourceId: string | null;
  status: ReportStatus;
  shareToken: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface ReportDetail extends Report {
  snapshotBlocks: { blocks?: CanvasBlock[] } | null;
}

export interface ReportCreatePayload {
  title: string;
  sourceType: ReportSourceType;
  sourceId?: string | null;
  snapshotBlocks?: { blocks?: CanvasBlock[] } | null;
}

export interface ReportShareResult {
  shareToken: string;
  shareUrl: string;
  expiresAt: string;
}

export interface ReportListParams extends PaginationParams {
  sourceType?: ReportSourceType;
  status?: ReportStatus;
}
