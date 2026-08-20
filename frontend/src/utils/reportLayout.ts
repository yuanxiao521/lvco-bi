/**
 * 报告式自动布局（模块 A）。
 *
 * 语义：
 * - h1 / h2 / text 叙事块 → 通栏（fullW），并把左右两列游标同时压到该块底部
 * - chart 图表块 → 双列网格（480x320），放进当前底部较低的列
 * - 游标从现有 blocks 无状态推导，天然兼容手动拖动后的任意布局
 */
import type { CanvasBlock } from "../types/canvas";

/** 报告式布局常量：双列 480 + 20 间距 + 480 = 980 内容宽 */
export const REPORT = {
  marginX: 20,
  gapY: 24,
  colGap: 20,
  chartW: 480,
  chartH: 320,
  fullW: 980,
  startY: 180,
} as const;

/** 左右两列各自的"已占用底部"游标 */
export interface LayoutCursor {
  leftBottom: number;
  rightBottom: number;
}

const LEFT_X = REPORT.marginX; // 20
const RIGHT_X = REPORT.marginX + REPORT.chartW + REPORT.colGap; // 520
const MID_X = RIGHT_X - REPORT.colGap / 2; // 列归属判定中线：510

/** 文本块的估算高度（渲染为 auto，这里仅用于游标推进） */
export function estimateBlockHeight(block: CanvasBlock): number {
  const t = (block as { type?: string }).type;
  if (t === "h1") return 56;
  if (t === "h2") return 44;
  if (t === "text") return 72;
  if (t === "chart") return typeof block.height === "number" ? block.height : REPORT.chartH;
  const h = (block as { height?: unknown }).height;
  return typeof h === "number" ? h : 300;
}

const isPlaced = (b: CanvasBlock) => typeof b.x === "number" && typeof b.y === "number";
const bottomOf = (b: CanvasBlock) => (b.y as number) + estimateBlockHeight(b);

/** 从现有 blocks 推导双列游标；无任何已定位块时返回 startY 起点 */
export function deriveCursor(blocks: CanvasBlock[], startY: number = REPORT.startY): LayoutCursor {
  let leftBottom = startY;
  let rightBottom = startY;
  for (const b of blocks) {
    if (!isPlaced(b)) continue;
    const w = typeof b.width === "number" ? b.width : REPORT.chartW;
    const bottom = bottomOf(b);
    if (w >= REPORT.fullW - 40) {
      // 通栏块：两列游标同时压到底部
      leftBottom = Math.max(leftBottom, bottom);
      rightBottom = Math.max(rightBottom, bottom);
    } else if ((b.x as number) + w / 2 < MID_X) {
      leftBottom = Math.max(leftBottom, bottom);
    } else {
      rightBottom = Math.max(rightBottom, bottom);
    }
  }
  return { leftBottom, rightBottom };
}

/** 下一个图表块位置：放进底部较低的列（左列优先平局） */
export function nextChartSlot(blocks: CanvasBlock[], startY: number = REPORT.startY): { x: number; y: number } {
  const { leftBottom, rightBottom } = deriveCursor(blocks, startY);
  if (leftBottom <= rightBottom) {
    return { x: LEFT_X, y: leftBottom > startY ? leftBottom + REPORT.gapY : leftBottom };
  }
  return { x: RIGHT_X, y: rightBottom > startY ? rightBottom + REPORT.gapY : rightBottom };
}

/** 下一个通栏块位置：两列最深底部 + 间距 */
export function nextFullWidthSlot(blocks: CanvasBlock[], startY: number = REPORT.startY): { x: number; y: number } {
  const { leftBottom, rightBottom } = deriveCursor(blocks, startY);
  const maxY = Math.max(leftBottom, rightBottom);
  return { x: LEFT_X, y: maxY > startY ? maxY + REPORT.gapY : maxY };
}

/**
 * 全量重排（arrange_layout）：按现有顺序重写所有块坐标。
 * - h1/h2/text → 通栏 fullW，并把游标整体压到该块底部（重置双列）
 * - chart → 双列网格 chartW x chartH
 * - image → 保持原宽，高度按比例（无高度则 300），按通栏处理
 */
export function applyReportLayout(blocks: CanvasBlock[], startY: number = REPORT.startY): CanvasBlock[] {
  let leftBottom = startY;
  let rightBottom = startY;
  const out = blocks.map((b) => {
    const type = (b as { type?: string }).type;
    const h = estimateBlockHeight(b);

    if (type === "chart") {
      // 双列：放底部较低的列
      let x: number, y: number;
      if (leftBottom <= rightBottom) {
        x = LEFT_X;
        y = leftBottom > startY ? leftBottom + REPORT.gapY : leftBottom;
        leftBottom = y + REPORT.chartH;
      } else {
        x = RIGHT_X;
        y = rightBottom > startY ? rightBottom + REPORT.gapY : rightBottom;
        rightBottom = y + REPORT.chartH;
      }
      return { ...b, x, y, width: REPORT.chartW, height: REPORT.chartH };
    }

    // 通栏块（h1/h2/text/image/未知）
    const maxY = Math.max(leftBottom, rightBottom);
    const y = maxY > startY ? maxY + REPORT.gapY : maxY;
    const w = type === "image" && typeof b.width === "number" ? b.width : REPORT.fullW;
    const bottom = y + h;
    leftBottom = bottom;
    rightBottom = bottom;
    return { ...b, x: LEFT_X, y, width: w };
  });
  return out;
}
