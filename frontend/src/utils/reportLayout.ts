/**
 * 报告式自动布局（模块 A）。
 *
 * 语义：
 * - h1 / h2 / text 叙事块 → 通栏（画布实宽），并把左右两列游标同时压到该块底部
 * - chart 图表块 → 双列网格（列宽按画布实宽均分），放进当前底部较低的列
 * - 画布过窄（单列放不下 minColW）时自动降为单列，图表也通栏，避免横向溢出
 * - 游标从现有 blocks 无状态推导，天然兼容手动拖动后的任意布局
 *
 * 几何不再写死 980：调用方把「画布内容区实宽」通过 LayoutOpts.width 传进来，
 * 由 resolveGeometry 推导列数/列宽；未传时用 fallbackW 兜底（等价旧版固定布局）。
 */
import type { CanvasBlock } from "../types/canvas";

/** 报告式布局常量（列宽由 resolveGeometry 按画布实宽推导，不在此写死） */
export const REPORT = {
  marginX: 20,     // 画布左右留白
  gapY: 24,        // 块间距
  colGap: 20,      // 双列列间距
  startY: 32,      // 首块起始 y（原为 180，会在画布顶部留一大片死区）
  chartH: 320,     // 图表块默认高度
  minColW: 360,    // 双列时每列最小宽度，低于则降为单列
  fallbackW: 980,  // 未测量到画布实宽时的兜底通栏宽
} as const;

/** 画布几何：按画布内容区实宽推导（列数 / 列宽 / 列 x） */
export interface LayoutGeometry {
  fullW: number;   // 通栏块宽度
  chartW: number;  // 图表块宽度（单列时 = fullW）
  cols: 1 | 2;
  leftX: number;
  rightX: number;
  midX: number;    // 列归属判定中线
}

/** 由画布内容区实宽推导几何；width 缺省/非法时回退 fallbackW */
export function resolveGeometry(width?: number): LayoutGeometry {
  const avail = Math.round(
    width && width > 0 ? Math.max(320, width - REPORT.marginX * 2) : REPORT.fallbackW,
  );
  const two = avail >= REPORT.minColW * 2 + REPORT.colGap;
  const chartW = two ? Math.round((avail - REPORT.colGap) / 2) : avail;
  return {
    fullW: avail,
    chartW,
    cols: two ? 2 : 1,
    leftX: REPORT.marginX,
    rightX: REPORT.marginX + chartW + REPORT.colGap,
    midX: REPORT.marginX + chartW + REPORT.colGap / 2,
  };
}

/** 布局参数：width = 画布内容区实宽（px）；startY 一般无需传 */
export interface LayoutOpts {
  width?: number;
  startY?: number;
}

/** 左右两列各自的"已占用底部"游标 */
export interface LayoutCursor {
  leftBottom: number;
  rightBottom: number;
}

/** 文本块渲染参数（与 CanvasBlocks 中 className 一致） */
const TEXT_FONT_PX = 14;        // 正文 14px
const TEXT_LINE_HEIGHT = 22;    // leading-relaxed ≈ 14*1.5
const TEXT_H_PADDING = 20;      // 块内左右 padding（p-5）
const TEXT_V_PADDING = 40;      // 块内上下 padding + 标签条空间

/** 估算文本内容渲染后的实际高度（每行可容纳字符数，中文字符约等于字号宽度） */
export function estimateTextHeight(content: string, widthPx: number, fontSize = TEXT_FONT_PX): number {
  const text = (content ?? "").replace(/\n/g, "\n");
  const usableWidth = Math.max(80, (widthPx || REPORT.fallbackW) - TEXT_H_PADDING * 2);
  const charsPerLine = Math.max(8, Math.floor(usableWidth / fontSize));
  let lines = 0;
  for (const seg of text.split("\n")) {
    lines += Math.max(1, Math.ceil((seg.length || 1) / charsPerLine));
  }
  return lines * TEXT_LINE_HEIGHT + TEXT_V_PADDING;
}

/**
 * 给块分配稳定可见编号（A1/A2/B1…），与后端 `canvas_tools._assign_block_labels` 同规则：
 * 按坐标排序（先 y 后 x），同一行（y 容差 10px）归为一行，行字母 A→Z，行内按 x 升序编号；
 * 无坐标的块排在最后继续累加。
 *
 * 两边必须一致：Agent 上下文里出现的 [A1] 就是用户在这个角标上看到的 A1，
 * 用户说"把 A1 改成折线图"才能落到正确的块上。
 *
 * @returns 与 blocks 下标一一对应的编号数组（空字符串表示未编号）
 */
export function assignBlockLabels(blocks: CanvasBlock[]): string[] {
  const out: string[] = new Array(blocks.length).fill("");
  const posOf = (b: CanvasBlock): [number, number] | null => {
    const d = b as { x?: unknown; y?: unknown };
    return typeof d.x === "number" && typeof d.y === "number" ? [d.x, d.y] : null;
  };
  const order = blocks
    .map((b, i) => ({ i, p: posOf(b) }))
    .sort((a, b) => {
      if (!a.p || !b.p) return (a.p ? 0 : 1) - (b.p ? 0 : 1); // 无坐标排最后
      if (Math.abs(a.p[1] - b.p[1]) <= 10) return a.p[0] - b.p[0]; // 同一行按 x
      return a.p[1] - b.p[1]; // 按 y 分行
    });

  const rowKeys: number[] = [];
  const rowLetters = new Map<number, string>();
  const colIdx = new Map<string, number>();
  let extraRows = 0;
  for (const { i, p } of order) {
    let row: string;
    if (p) {
      let key = rowKeys.find((k) => Math.abs(k - p[1]) <= 10);
      if (key === undefined) {
        key = p[1];
        rowKeys.push(key);
      }
      if (!rowLetters.has(key)) rowLetters.set(key, String.fromCharCode(65 + rowLetters.size));
      row = rowLetters.get(key) as string;
    } else {
      // 无坐标块：行字母取当前行之后的新行（用负数占位键避免与真实 y 冲突）
      row = String.fromCharCode(65 + rowLetters.size);
      rowLetters.set(-1 - extraRows++, row);
    }
    const col = (colIdx.get(row) ?? 0) + 1;
    colIdx.set(row, col);
    out[i] = `${row}${col}`;
  }
  return out;
}

/** 块的高度估算：显式 height 优先（BlockWrapper 就是按它渲染的），否则按内容估算 */
export function estimateBlockHeight(block: CanvasBlock, geo: LayoutGeometry = resolveGeometry()): number {
  // 关键：用户拖过高度/带默认尺寸的块，渲染高度 = block.height；
  // 若这里仍按内容估算（如 h1 算成 58px，实际 250px），后续块会直接叠上去
  const explicit = (block as { height?: unknown }).height;
  if (typeof explicit === "number" && explicit > 0) return explicit;

  const t = (block as { type?: string }).type;
  if (t === "h1") {
    const c = (block as { content?: string }).content || "";
    // h1 通常一行（22px 字），内容超长时按字数折行
    return c ? Math.ceil((c.length || 1) / 40) * 34 + 24 : 56;
  }
  if (t === "h2") {
    const c = (block as { content?: string }).content || "";
    return c ? Math.ceil((c.length || 1) / 50) * 30 + 20 : 44;
  }
  if (t === "text") {
    const c = (block as { content?: string }).content || "";
    const w = typeof block.width === "number" ? block.width : geo.fullW;
    return estimateTextHeight(c, w);
  }
  if (t === "chart") return REPORT.chartH;
  return 300;
}

/** 块已占用的底部 y（用于估算画布实际内容高度，撑开滚动区） */
export function blockBottom(block: CanvasBlock, index = 0, geo: LayoutGeometry = resolveGeometry()): number {
  const d = block as { x?: unknown; y?: unknown };
  const hasXY = typeof d.x === "number" && typeof d.y === "number";
  // 无坐标的块按与 CanvasBlocks 渲染一致的自动铺位估算
  const y = hasXY ? (d.y as number) : Math.floor(index / 2) * 400;
  return y + estimateBlockHeight(block, geo);
}

const isPlaced = (b: CanvasBlock) => typeof b.x === "number" && typeof b.y === "number";
const bottomOf = (b: CanvasBlock, geo: LayoutGeometry) => (b.y as number) + estimateBlockHeight(b, geo);

/** 去掉块的显式高度（叙事块交还给内容自适应） */
function withoutHeight(b: CanvasBlock): CanvasBlock {
  const rec = { ...(b as Record<string, unknown>) };
  delete rec.height;
  return rec as CanvasBlock;
}

/** 块的实际占宽：无 width 的块按类型推断（叙事块通栏、图表按列宽） */
function widthOf(b: CanvasBlock, geo: LayoutGeometry): number {
  if (typeof b.width === "number") return b.width;
  const t = String((b as { type?: unknown }).type ?? "");
  return t === "text" || t === "h1" || t === "h2" ? geo.fullW : geo.chartW;
}

/** 从现有 blocks 推导双列游标；无任何已定位块时返回 startY 起点 */
export function deriveCursor(blocks: CanvasBlock[], opts: LayoutOpts = {}): LayoutCursor {
  const geo = resolveGeometry(opts.width);
  const startY = opts.startY ?? REPORT.startY;
  let leftBottom = startY;
  let rightBottom = startY;
  for (const b of blocks) {
    if (!isPlaced(b)) continue;
    const w = widthOf(b, geo);
    const bottom = bottomOf(b, geo);
    if (w >= geo.fullW - 40) {
      // 通栏块：两列游标同时压到底部
      leftBottom = Math.max(leftBottom, bottom);
      rightBottom = Math.max(rightBottom, bottom);
    } else if ((b.x as number) + w / 2 < geo.midX) {
      leftBottom = Math.max(leftBottom, bottom);
    } else {
      rightBottom = Math.max(rightBottom, bottom);
    }
  }
  return { leftBottom, rightBottom };
}

/** 下一个图表块位置：放进底部较低的列（左列优先平局） */
export function nextChartSlot(blocks: CanvasBlock[], opts: LayoutOpts = {}): { x: number; y: number } {
  const geo = resolveGeometry(opts.width);
  const startY = opts.startY ?? REPORT.startY;
  const { leftBottom, rightBottom } = deriveCursor(blocks, opts);
  if (leftBottom <= rightBottom) {
    return { x: geo.leftX, y: leftBottom > startY ? leftBottom + REPORT.gapY : leftBottom };
  }
  return { x: geo.rightX, y: rightBottom > startY ? rightBottom + REPORT.gapY : rightBottom };
}

/** 下一个通栏块位置：两列最深底部 + 间距 */
export function nextFullWidthSlot(blocks: CanvasBlock[], opts: LayoutOpts = {}): { x: number; y: number } {
  const geo = resolveGeometry(opts.width);
  const startY = opts.startY ?? REPORT.startY;
  const { leftBottom, rightBottom } = deriveCursor(blocks, opts);
  const maxY = Math.max(leftBottom, rightBottom);
  return { x: geo.leftX, y: maxY > startY ? maxY + REPORT.gapY : maxY };
}

/**
 * 全量重排（arrange_layout）：按现有顺序重写所有块坐标。
 * - h1/h2/text → 通栏 fullW，并把游标整体压到该块底部（重置双列）
 * - chart → 双列网格 chartW x chartH（单列模式下列宽 = fullW）
 * - image → 保持原宽（超过通栏宽则收窄），按通栏处理
 */
export function applyReportLayout(blocks: CanvasBlock[], opts: LayoutOpts = {}): CanvasBlock[] {
  const geo = resolveGeometry(opts.width);
  const startY = opts.startY ?? REPORT.startY;
  let leftBottom = startY;
  let rightBottom = startY;
  const out = blocks.map((b) => {
    const type = (b as { type?: string }).type;

    if (type === "chart") {
      // 网格：放底部较低的列（单列模式恒为左列通栏）
      const useLeft = geo.cols === 1 || leftBottom <= rightBottom;
      const colBottom = useLeft ? leftBottom : rightBottom;
      const y = colBottom > startY ? colBottom + REPORT.gapY : colBottom;
      if (useLeft) leftBottom = y + REPORT.chartH;
      else rightBottom = y + REPORT.chartH;
      return { ...b, x: useLeft ? geo.leftX : geo.rightX, y, width: geo.chartW, height: REPORT.chartH };
    }

    // 通栏块（h1/h2/text/image/未知）：
    // 叙事块重置为自适应高度（否则工具栏建的 250 高空框会一直留着，还撑歪后续块），
    // 图片/其他块保留自身高度
    const cleaned = type === "image" ? b : withoutHeight(b);
    const h = estimateBlockHeight(cleaned, geo);
    const maxY = Math.max(leftBottom, rightBottom);
    const y = maxY > startY ? maxY + REPORT.gapY : maxY;
    // 宽度：叙事块一律通栏（报告每段等宽才整齐）；图片保留原宽但不超过通栏宽
    const originW = typeof b.width === "number" ? b.width : undefined;
    const w = type === "image" && originW ? Math.min(originW, geo.fullW) : geo.fullW;
    const bottom = y + h;
    leftBottom = bottom;
    rightBottom = bottom;
    return { ...cleaned, x: geo.leftX, y, width: w };
  });
  return out;
}