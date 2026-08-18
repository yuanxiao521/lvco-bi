import apiClient, { unwrapApi } from "./client";
import type {
  PaginatedResult,
  Report,
  ReportCreatePayload,
  ReportDetail,
  ReportListParams,
  ReportShareResult,
  ReportStatus,
} from "./types";

export async function listReports(
  params: ReportListParams = {}
): Promise<PaginatedResult<Report>> {
  const response = await apiClient.get("/reports", { params });
  return unwrapApi<PaginatedResult<Report>>(response.data);
}

export async function createReport(payload: ReportCreatePayload): Promise<Report> {
  const response = await apiClient.post("/reports", payload);
  return unwrapApi<Report>(response.data);
}

export async function getReport(id: string): Promise<ReportDetail> {
  const response = await apiClient.get(`/reports/${id}`);
  return unwrapApi<ReportDetail>(response.data);
}

export async function updateReport(
  id: string,
  payload: { title: string }
): Promise<Report> {
  const response = await apiClient.patch(`/reports/${id}`, payload);
  return unwrapApi<Report>(response.data);
}

export async function updateReportStatus(
  id: string,
  status: ReportStatus
): Promise<Report> {
  const response = await apiClient.patch(`/reports/${id}/status`, { status });
  return unwrapApi<Report>(response.data);
}

export async function shareReport(id: string): Promise<ReportShareResult> {
  const response = await apiClient.post(`/reports/${id}/share`);
  return unwrapApi<ReportShareResult>(response.data);
}

export async function exportReportHtml(id: string): Promise<Blob> {
  const response = await apiClient.get(`/reports/${id}/export/pdf`, {
    responseType: "blob",
  });
  return response.data as Blob;
}

export async function downloadReport(id: string, title: string): Promise<void> {
  // Try blob first for PDF response
  try {
    const blobResp = await apiClient.get(`/reports/${id}/export/pdf`, {
      responseType: "blob",
    });

    const blob = blobResp.data as Blob;

    // If backend returns JSON (MinIO URL), handle it
    if (blob.type === "application/json" || blob.type.includes("json")) {
      const text = await blob.text();
      try {
        const json = JSON.parse(text);
        if (json.url) {
          window.open(json.url, "_blank");
          return;
        }
      } catch { /* not JSON, fall through */ }
    }

    // PDF or HTML blob direct download
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeTitle = title.replace(/[\\/:*?"<>|]/g, "_");
    a.download = `${safeTitle}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  } catch {
    // Fallback: try text approach for JSON responses
    const resp = await apiClient.get(`/reports/${id}/export/pdf`, {
      responseType: "text",
      transformResponse: [(data) => data],
    });

    const rawText = resp.data as string;

    if (rawText.trim().startsWith("{")) {
      try {
        const json = JSON.parse(rawText);
        if (json.url) {
          window.open(json.url, "_blank");
          return;
        }
      } catch { /* not JSON, treat as HTML */ }
    }

    const blob = new Blob(["\uFEFF" + rawText], { type: "text/html;charset=UTF-8" });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeTitle = title.replace(/[\\/:*?"<>|]/g, "_");
    a.download = `${safeTitle}.pdf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  }
}

export async function deleteReport(id: string): Promise<void> {
  await apiClient.delete(`/reports/${id}`);
}