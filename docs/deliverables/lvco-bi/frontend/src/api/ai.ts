import apiClient, { unwrapApi } from "./client";
import type {
  AICleanRequest,
  AICleanResult,
  AIRecommendRequest,
  AIRecommendResult,
  AIMessage,
  AISession,
  AISessionDetail,
} from "./types";

export async function listSessions(): Promise<AISession[]> {
  const response = await apiClient.get("/ai/sessions");
  return unwrapApi<AISession[]>(response.data);
}

export async function createSession(title?: string): Promise<AISession> {
  const response = await apiClient.post("/ai/sessions", { title });
  return unwrapApi<AISession>(response.data);
}

export async function getSession(sessionId: string): Promise<AISessionDetail> {
  const response = await apiClient.get(`/ai/sessions/${sessionId}`);
  return unwrapApi<AISessionDetail>(response.data);
}

export async function deleteSession(sessionId: string): Promise<void> {
  await apiClient.delete(`/ai/sessions/${sessionId}`);
}

export async function listMessages(sessionId: string): Promise<AIMessage[]> {
  const response = await apiClient.get(`/ai/sessions/${sessionId}/messages`);
  return unwrapApi<AIMessage[]>(response.data);
}

export async function sendMessage(
  sessionId: string,
  content: string
): Promise<AIMessage> {
  const response = await apiClient.post(
    `/ai/sessions/${sessionId}/messages`,
    { content }
  );
  return unwrapApi<AIMessage>(response.data);
}

export async function cleanData(payload: AICleanRequest): Promise<AICleanResult> {
  const response = await apiClient.post("/ai/clean", payload);
  return unwrapApi<AICleanResult>(response.data);
}

export async function recommendCharts(
  payload: AIRecommendRequest
): Promise<AIRecommendResult> {
  const response = await apiClient.post("/ai/recommend", payload);
  return unwrapApi<AIRecommendResult>(response.data);
}

// POST /ai/insights
export async function generateInsights(payload: {
  datasource_id: string;
  query_config: Record<string, unknown>;
}): Promise<{
  insights: Array<{
    type: "trend" | "anomaly" | "opportunity";
    title: string;
    description: string;
    severity: string;
    related_fields?: string[];
  }>;
}> {
  const response = await apiClient.post("/ai/insights", payload);
  return unwrapApi<{
    insights: Array<{
      type: "trend" | "anomaly" | "opportunity";
      title: string;
      description: string;
      severity: string;
      related_fields?: string[];
    }>;
  }>(response.data);
}

// POST /ai/polish
export async function polishText(
  text: string,
  style: string
): Promise<{
  original: string;
  polished: string;
  style: string;
}> {
  const response = await apiClient.post("/ai/polish", { text, style });
  return unwrapApi<{
    original: string;
    polished: string;
    style: string;
  }>(response.data);
}