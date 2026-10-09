/**
 * 画布换数据源时的图表重映射。
 *
 * 背景：画布是"画布级单一数据源"（Canvas.datasourceId），图表配置里的
 * dimensions / measures 都是该数据源的字段名，换源后这些引用全部失效。
 * 与其让它们带着旧源的字段去查询（必然报"字段不存在"），不如：
 * - 保留块本身（标题/图表类型/位置不变，用户的排版不白费）
 * - 清空维度与度量、丢弃旧源的查询结果
 * - 把配置的 datasourceId 指向新源，等待用户重新选字段（"待重配"）
 */
import type { CanvasBlock } from "../types/canvas";
import type { ChartQueryConfig, QueryResult } from "../types/chart";

export interface DatasourceSwitchResult {
  /** 重映射后的图表配置（仅图表块，键仍是 blockId） */
  chartConfigs: Record<string, ChartQueryConfig>;
  /** 重映射后的查询结果：受影响块的旧结果被丢弃，避免显示上一个数据源的数据 */
  chartResults: Record<string, QueryResult>;
  /** 受影响的图表块数量（用于提示"n 张图需重新配置"） */
  affected: number;
}

/** 把画布上的图表配置/结果重映射到新数据源（返回新对象，不修改入参） */
export function remapChartsForDatasource(
  blocks: CanvasBlock[],
  chartConfigs: Record<string, ChartQueryConfig>,
  chartResults: Record<string, QueryResult>,
  nextDatasourceId: string | null,
): DatasourceSwitchResult {
  const nextConfigs: Record<string, ChartQueryConfig> = { ...chartConfigs };
  const nextResults: Record<string, QueryResult> = { ...chartResults };
  let affected = 0;

  for (const block of blocks) {
    const rec = block as Record<string, unknown>;
    if (rec.type !== "chart") continue;
    const blockId = typeof rec.blockId === "string" ? rec.blockId : "";
    if (!blockId) continue;

    const prev = nextConfigs[blockId];
    nextConfigs[blockId] = {
      dimensions: [],
      measures: [],
      filters: [],
      chartType: (prev?.chartType ?? (rec.chartType as ChartQueryConfig["chartType"])) || "bar",
      limit: prev?.limit ?? 20,
      datasourceId: nextDatasourceId ?? undefined,
    };
    // 旧源的数据不能留在画布上，charts 会显示"请重新选择维度/度量"的空态
    delete nextResults[blockId];
    affected += 1;
  }

  return { chartConfigs: nextConfigs, chartResults: nextResults, affected };
}