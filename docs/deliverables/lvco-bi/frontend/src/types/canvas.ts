export type ChartType =
  | "bar"
  | "line"
  | "pie"
  | "scatter"
  | "area"
  | "donut"
  | "funnel"
  | "heatmap"
  | "radar"
  | "sankey"
  | "grouped_bar"
  | "stacked_bar"
  | "horizontal_bar"
  | "kpi_card";

/** ChartType → 中文名（用于图例 / 卡片 / 推荐列表等任何需要展示类型名的地方） */
export const CHART_TYPE_LABELS: Record<ChartType, string> = {
  bar: "柱状图",
  line: "折线图",
  pie: "饼图",
  scatter: "散点图",
  area: "面积图",
  donut: "环形图",
  funnel: "漏斗图",
  heatmap: "热力图",
  radar: "雷达图",
  sankey: "桑基图",
  grouped_bar: "分组柱状图",
  stacked_bar: "堆叠柱状图",
  horizontal_bar: "水平条形图",
  kpi_card: "KPI 卡片",
};

export interface PalettePreset {
  id: string;
  name: string;
  colors: string[];
}

export const PALETTE_PRESETS: PalettePreset[] = [
  { id: "default", name: "默认（青绿）", colors: ["#2BB5A0", "#6C7BF2", "#F5A623", "#EF5B5B", "#4EADFF", "#A78BFA"] },
  { id: "warm", name: "暖色（橙红）", colors: ["#EF5B5B", "#F5A623", "#FFB347", "#E2725B", "#C44536", "#FF7F50"] },
  { id: "cool", name: "冷色（蓝紫）", colors: ["#6C7BF2", "#4EADFF", "#A78BFA", "#5B8FF9", "#5AD8A6", "#7B9DFF"] },
  { id: "rainbow", name: "彩虹", colors: ["#EF5B5B", "#F5A623", "#2BB5A0", "#4EADFF", "#6C7BF2", "#A78BFA"] },
  { id: "mono", name: "单色（青绿）", colors: ["#2BB5A0", "#3DCBB5", "#5BDCC5", "#7DE8D2", "#A4F0E0", "#C5F7EE"] },
  { id: "earth", name: "大地（土色）", colors: ["#8B7355", "#A0826D", "#BC8F6F", "#C9A57B", "#D2B48C", "#DEB887"] },
];

export function getPaletteById(id: string | undefined | null): string[] {
  const found = PALETTE_PRESETS.find((p) => p.id === id);
  return found ? found.colors : PALETTE_PRESETS[0].colors;
}

export interface TextBlock {
  type: "text" | "h1" | "h2";
  content: string;
}

export interface DividerBlock {
  type: "divider";
}

export interface ChartBlock {
  type: "chart";
  blockId: string;
  title?: string;
  renderer?: string;
  palette?: string;
  height?: number;
}

export interface ImageBlock {
  type: "image";
  src: string;
  alt?: string;
  width?: number;
  height?: number;
}

export type CanvasBlock = TextBlock | DividerBlock | ChartBlock | ImageBlock | Record<string, unknown>;

export interface Canvas {
  id: string;
  userId: string;
  datasourceId: string | null;
  tableName: string | null;
  title: string;
  blocks: CanvasBlock[] | null;
  createdAt: string;
  updatedAt: string | null;
}

export interface CanvasCreatePayload {
  title: string;
  datasourceId: string;
  tableName?: string | null;
}

export interface CanvasUpdateBlocksPayload {
  blocks: CanvasBlock[];
}

export interface MeasureConfig {
  field: string;
  agg: "SUM" | "AVG" | "COUNT" | "MAX" | "MIN" | "STDDEV" | "MEDIAN" | "COUNT_DISTINCT";
}

export type FilterOp =
  | "eq"
  | "neq"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "between"
  | "in"
  | "like";

export interface FilterConfig {
  field: string;
  op: FilterOp;
  value: unknown;
}

export interface SortConfig {
  field: string;
  order?: "asc" | "desc";
}
