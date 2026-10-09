/**
 * 系统默认模板
 * - 每个模板预置了 chartConfig（数据源ID + 维度 + 度量 + 图表类型）
 * - 加载时 FreeCanvas 保留 chartConfig，自动触发查询渲染图表
 * - 用户可以切换数据源后重新查询
 * - 每个 block 都有 x/y/width 坐标，确保模板加载后块整齐排列
 */
import type { CanvasBlock } from "../types/canvas";
import type { ChartQueryConfig } from "../types/chart";

// ─── 真实数据源 ID（来自数据库 datasources 表） ───
const DS_ECOMMERCE = "12314951-305f-4251-a24e-d3012ce34077"; // Ecommerce Orders
const DS_CUSTOMER = "9a39055a-e9fc-41cd-9ff2-9dd824d3e499";   // Customer Metrics
const DS_EMPLOYEE = "ecdb73d8-17b2-498a-bdd5-ee04a5a795b6";  // Employee Sales

export interface DefaultTemplate {
  id: string;
  title: string;
  desc: string;
  category: "销售" | "用户" | "运营" | "财务" | "通用";
  iconName: "TrendingUp" | "Users" | "Activity" | "Wallet";
  color: "primary" | "info" | "success" | "warning";
  blocks: CanvasBlock[];
  /** 每个图表块的预置查询配置，key 为 blockId */
  chartConfigs?: Record<string, ChartQueryConfig>;
  /** 模板默认数据源 ID */
  defaultDatasourceId?: string;
  suggestedFields: Array<{
    name: string;
    label: string;
    category: "dimension" | "measure" | "time";
  }>;
}

// ─── 布局常量 ───
const COL_W = 420;
const COL_GAP = 20;
const ROW_H = 300;
const TEXT_H = 100;
const H1_H = 90;
const H2_H = 70;
const DIV_H = 24;
const START_X = 40;
const START_Y = 30;
const FULL_W = COL_W * 2 + COL_GAP;

let _y = START_Y;
function ny(h: number) { const v = _y; _y += h + 16; return v; }
/** 占位但不推进 y（用于同行右侧块，避免多算一行的 gap） */
function peekY() { return _y; }
function resetY() { _y = START_Y; }

function chartBlock(blockId: string, title: string, x: number, y: number, w = COL_W, h = ROW_H): CanvasBlock {
  return {
    type: "chart", blockId, title,
    renderer: "echarts", palette: "default",
    width: w, height: h, x, y,
  } as unknown as CanvasBlock;
}

function textBlock(content: string, w = FULL_W, h = TEXT_H): CanvasBlock {
  return { type: "text", content, width: w, height: h, x: START_X, y: ny(h) };
}
function h1Block(content: string): CanvasBlock {
  return { type: "h1", content, width: FULL_W, height: H1_H, x: START_X, y: ny(H1_H) };
}
function h2Block(content: string): CanvasBlock {
  return { type: "h2", content, width: FULL_W, height: H2_H, x: START_X, y: ny(H2_H) };
}
function dividerBlock(): CanvasBlock {
  return { type: "divider", width: FULL_W, height: DIV_H, x: START_X, y: ny(DIV_H) };
}

export const DEFAULT_TEMPLATES: DefaultTemplate[] = [
  // ─── 空白模板 ───
  {
    id: "system-blank",
    title: "空白模板",
    desc: "从零开始，构建你的第一份分析报告",
    category: "通用",
    iconName: "TrendingUp",
    color: "primary",
    blocks: [
      { type: "h1", content: "新建分析报告", width: FULL_W, height: H1_H, x: START_X, y: START_Y },
      {
        type: "text",
        content: "欢迎使用 LvcoBI 自由画布！请从左侧选择数据源，然后在右侧面板配置图表字段，点击「生成图表」即可开始分析。",
        width: FULL_W, height: TEXT_H, x: START_X, y: START_Y + H1_H + 16,
      },
    ],
    suggestedFields: [],
  },

  // ─── 销售分析（Ecommerce Orders） ──
  {
    id: "system-sales",
    title: "销售分析",
    desc: "地区销售额对比 + 订单状态 + 品类分布",
    category: "销售",
    iconName: "TrendingUp",
    color: "primary",
    defaultDatasourceId: DS_ECOMMERCE,
    blocks: (() => {
      resetY();
      return [
        h1Block("销售分析报告"),
        textBlock("本报告基于电商订单数据，分析各地区销售表现、订单状态与产品类别分布情况，帮助识别高贡献区域和品类。"),
        dividerBlock(),
        h2Block("一、各地区销售表现"),
        (() => { const y = ny(ROW_H); return chartBlock("b1", "各地区销售额对比", START_X, y); })(),
        (() => { const y = peekY() - ROW_H - 16; return chartBlock("b2", "订单状态分布", START_X + COL_W + COL_GAP, y); })(),
        h2Block("二、产品类别分布"),
        chartBlock("b3", "品类销售额占比", START_X, ny(ROW_H), COL_W, ROW_H),
        chartBlock("b4", "品类订单量对比", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16, COL_W, ROW_H),
      ];
    })(),
    chartConfigs: {
      b1: { dimensions: ["region"], measures: [{ field: "total_amount", agg: "SUM" }], chartType: "bar", datasourceId: DS_ECOMMERCE, limit: 20 },
      b2: { dimensions: ["status"], measures: [{ field: "order_id", agg: "COUNT" }], chartType: "pie", datasourceId: DS_ECOMMERCE, limit: 20 },
      b3: { dimensions: ["category"], measures: [{ field: "total_amount", agg: "SUM" }], chartType: "donut", datasourceId: DS_ECOMMERCE, limit: 20 },
      b4: { dimensions: ["category"], measures: [{ field: "order_id", agg: "COUNT" }], chartType: "bar", datasourceId: DS_ECOMMERCE, limit: 20 },
    },
    suggestedFields: [
      { name: "region", label: "地区", category: "dimension" },
      { name: "category", label: "产品类别", category: "dimension" },
      { name: "total_amount", label: "销售额", category: "measure" },
      { name: "status", label: "订单状态", category: "dimension" },
    ],
  },

  // ─── 用户增长分析（Customer Metrics） ───
  {
    id: "system-user-growth",
    title: "用户增长分析",
    desc: "客户分层 + 消费分布 + 流失风险",
    category: "用户",
    iconName: "Users",
    color: "info",
    defaultDatasourceId: DS_CUSTOMER,
    blocks: (() => {
      resetY();
      return [
        h1Block("用户增长分析报告"),
        textBlock("聚焦客户规模变化趋势、各渠道获客质量与用户分层结构，识别增长瓶颈与机会点。"),
        dividerBlock(),
        h2Block("一、客户分层结构"),
        chartBlock("b1", "客户忠诚度分层", START_X, ny(ROW_H)),
        chartBlock("b2", "流失风险分布", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16),
        h2Block("二、消费行为分析"),
        chartBlock("b3", "各地区客户消费总额", START_X, ny(ROW_H)),
        chartBlock("b4", "各地区客户订单数", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16),
      ];
    })(),
    chartConfigs: {
      b1: { dimensions: ["loyalty_tier"], measures: [{ field: "customer_id", agg: "COUNT" }], chartType: "pie", datasourceId: DS_CUSTOMER, limit: 20 },
      b2: { dimensions: ["churn_risk"], measures: [{ field: "customer_id", agg: "COUNT" }], chartType: "donut", datasourceId: DS_CUSTOMER, limit: 20 },
      b3: { dimensions: ["region"], measures: [{ field: "total_spent", agg: "SUM" }], chartType: "bar", datasourceId: DS_CUSTOMER, limit: 20 },
      b4: { dimensions: ["region"], measures: [{ field: "total_orders", agg: "SUM" }], chartType: "horizontal_bar", datasourceId: DS_CUSTOMER, limit: 20 },
    },
    suggestedFields: [
      { name: "region", label: "地区", category: "dimension" },
      { name: "loyalty_tier", label: "忠诚等级", category: "dimension" },
      { name: "churn_risk", label: "流失风险", category: "dimension" },
      { name: "total_spent", label: "总消费额", category: "measure" },
      { name: "total_orders", label: "总订单数", category: "measure" },
    ],
  },

  // ─── 运营概览（Employee Sales） ───
  {
    id: "system-ops",
    title: "运营概览",
    desc: "员工绩效看板 + 部门对比分析",
    category: "运营",
    iconName: "Activity",
    color: "success",
    defaultDatasourceId: DS_EMPLOYEE,
    blocks: (() => {
      resetY();
      return [
        h1Block("运营概览周报"),
        textBlock("本周核心运营指标一览，包含各地区员工绩效、部门达成率对比与佣金分析，辅助运营决策。"),
        dividerBlock(),
        h2Block("一、地区绩效对比"),
        chartBlock("b1", "各地区实际销售额", START_X, ny(ROW_H)),
        chartBlock("b2", "各地区目标达成率", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16),
        h2Block("二、部门分析"),
        chartBlock("b3", "各部门销售额对比", START_X, ny(ROW_H)),
        chartBlock("b4", "各部门佣金分布", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16),
      ];
    })(),
    chartConfigs: {
      b1: { dimensions: ["region"], measures: [{ field: "actual_sales", agg: "SUM" }], chartType: "bar", datasourceId: DS_EMPLOYEE, limit: 20 },
      b2: { dimensions: ["region"], measures: [{ field: "achievement_pct", agg: "AVG" }], chartType: "bar", datasourceId: DS_EMPLOYEE, limit: 20 },
      b3: { dimensions: ["department"], measures: [{ field: "actual_sales", agg: "SUM" }], chartType: "horizontal_bar", datasourceId: DS_EMPLOYEE, limit: 20 },
      b4: { dimensions: ["department"], measures: [{ field: "commission", agg: "SUM" }], chartType: "pie", datasourceId: DS_EMPLOYEE, limit: 20 },
    },
    suggestedFields: [
      { name: "region", label: "地区", category: "dimension" },
      { name: "department", label: "部门", category: "dimension" },
      { name: "actual_sales", label: "实际销售额", category: "measure" },
      { name: "achievement_pct", label: "达成率", category: "measure" },
      { name: "commission", label: "佣金", category: "measure" },
    ],
  },

  // ─── 财务分析（Employee Sales） ───
  {
    id: "system-finance",
    title: "财务分析",
    desc: "部门业绩 + 目标达成 + 佣金分析",
    category: "财务",
    iconName: "Wallet",
    color: "warning",
    defaultDatasourceId: DS_EMPLOYEE,
    blocks: (() => {
      resetY();
      return [
        h1Block("财务经营分析"),
        textBlock("聚焦各部门业绩表现、目标达成率与佣金支出，结合地区维度交叉分析，识别盈利质量波动。"),
        dividerBlock(),
        h2Block("一、部门业绩概览"),
        chartBlock("b1", "各部门销售额 vs 目标", START_X, ny(ROW_H)),
        chartBlock("b2", "各部门达成率分布", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16),
        h2Block("二、地区财务对比"),
        chartBlock("b3", "各地区销售额排名", START_X, ny(ROW_H)),
        chartBlock("b4", "各地区佣金支出", START_X + COL_W + COL_GAP, peekY() - ROW_H - 16),
      ];
    })(),
    chartConfigs: {
      b1: { dimensions: ["department"], measures: [{ field: "actual_sales", agg: "SUM" }, { field: "monthly_target", agg: "SUM" }], chartType: "grouped_bar", datasourceId: DS_EMPLOYEE, limit: 20 },
      b2: { dimensions: ["department"], measures: [{ field: "achievement_pct", agg: "AVG" }], chartType: "bar", datasourceId: DS_EMPLOYEE, limit: 20 },
      b3: { dimensions: ["region"], measures: [{ field: "actual_sales", agg: "SUM" }], chartType: "horizontal_bar", datasourceId: DS_EMPLOYEE, limit: 20 },
      b4: { dimensions: ["region"], measures: [{ field: "commission", agg: "SUM" }], chartType: "bar", datasourceId: DS_EMPLOYEE, limit: 20 },
    },
    suggestedFields: [
      { name: "department", label: "部门", category: "dimension" },
      { name: "region", label: "地区", category: "dimension" },
      { name: "actual_sales", label: "实际销售额", category: "measure" },
      { name: "monthly_target", label: "月目标", category: "measure" },
      { name: "achievement_pct", label: "达成率", category: "measure" },
      { name: "commission", label: "佣金", category: "measure" },
    ],
  },
];

export function findDefaultTemplate(id: string | null | undefined): DefaultTemplate | undefined {
  if (!id || !id.startsWith("system-")) return undefined;
  return DEFAULT_TEMPLATES.find((t) => t.id === id);
}

export function isSystemTemplateId(id: string | null | undefined): boolean {
  return !!id && id.startsWith("system-");
}
