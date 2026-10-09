import apiClient from "./client";

export interface TrashItem {
  id: string;
  title: string;
  type: "canvas" | "dashboard" | "report";
  deletedAt: string | null;
}

export interface TrashListResult {
  items: TrashItem[];
  total: number;
}

export async function listTrash(): Promise<TrashListResult> {
  const response = await apiClient.get("/trash");
  return response.data?.data as TrashListResult;
}

export async function restoreTrashItem(
  itemType: string,
  itemId: string,
): Promise<void> {
  await apiClient.post(`/trash/${itemType}/${itemId}/restore`);
}

export async function permanentDeleteTrashItem(
  itemType: string,
  itemId: string,
): Promise<void> {
  await apiClient.delete(`/trash/${itemType}/${itemId}`);
}
