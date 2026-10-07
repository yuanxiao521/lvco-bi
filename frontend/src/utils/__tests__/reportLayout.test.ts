import { describe, it, expect } from 'vitest';
import {
  REPORT,
  resolveGeometry,
  deriveCursor,
  nextChartSlot,
  nextFullWidthSlot,
  applyReportLayout,
  estimateBlockHeight,
  assignBlockLabels,
} from '../../utils/reportLayout';
import type { CanvasBlock } from '../../types/canvas';

const chart = (x?: number, y?: number): CanvasBlock =>
  ({ type: 'chart', blockId: 'c1', x, y, width: 480, height: REPORT.chartH } as CanvasBlock);
const text = (content = '叙事', x?: number, y?: number): CanvasBlock =>
  ({ type: 'text', content, x, y } as CanvasBlock);
const h1 = (content = '标题'): CanvasBlock => ({ type: 'h1', content } as CanvasBlock);

/** 默认几何（未传画布宽 → 兜底 980 通栏，等价旧版固定布局） */
const GEO = resolveGeometry();

describe('resolveGeometry', () => {
  it('宽画布：双列均分列宽', () => {
    const geo = resolveGeometry(1440); // 通栏 1400，列宽 (1400-20)/2 = 690
    expect(geo.cols).toBe(2);
    expect(geo.fullW).toBe(1400);
    expect(geo.chartW).toBe(690);
    expect(geo.leftX).toBe(20);
    expect(geo.rightX).toBe(730);
  });

  it('窄画布：降为单列，图表通栏（不横向溢出）', () => {
    const geo = resolveGeometry(700); // 通栏 660 < 双列最小需求 740
    expect(geo.cols).toBe(1);
    expect(geo.chartW).toBe(660);
    expect(geo.fullW).toBe(660);
  });

  it('未传宽：回退兜底通栏 980（列宽 480）', () => {
    expect(GEO.fullW).toBe(REPORT.fallbackW);
    expect(GEO.cols).toBe(2);
    expect(GEO.chartW).toBe(480);
  });
});

describe('deriveCursor', () => {
  it('空画布返回起点', () => {
    expect(deriveCursor([])).toEqual({ leftBottom: REPORT.startY, rightBottom: REPORT.startY });
  });

  it('通栏文本块把两列游标同时压到底部', () => {
    const t = text('x', 20, 100);
    const bottom = 100 + estimateBlockHeight(t); // > startY(32)
    const c = deriveCursor([t]);
    expect(c.leftBottom).toBe(bottom);
    expect(c.rightBottom).toBe(bottom);
  });

  it('左右列分别追踪', () => {
    const left = chart(20, 200); // 左列底部 200+320=520
    const right = chart(520, 400); // 右列底部 400+320=720
    const c = deriveCursor([left, right]);
    expect(c.leftBottom).toBe(520);
    expect(c.rightBottom).toBe(720);
  });
});

describe('nextChartSlot', () => {
  it('空画布放左列起点', () => {
    expect(nextChartSlot([])).toEqual({ x: 20, y: REPORT.startY });
  });

  it('第二张图放右列同行', () => {
    const slot = nextChartSlot([chart(20, REPORT.startY)]);
    expect(slot).toEqual({ x: 520, y: REPORT.startY });
  });

  it('左右都有图时放较浅的列并加间距', () => {
    const left = chart(20, REPORT.startY); // 左底 startY+320
    const right = chart(520, 400); // 右底 720
    const slot = nextChartSlot([left, right]);
    expect(slot).toEqual({ x: 20, y: REPORT.startY + 320 + REPORT.gapY });
  });

  it('按传入画布宽取右列 x（自适应）', () => {
    const geo = resolveGeometry(1440);
    const slot = nextChartSlot([chart(geo.leftX, REPORT.startY)], { width: 1440 });
    expect(slot.x).toBe(geo.rightX);
  });

  it('单列模式：第二张图不落右列，改落下一行', () => {
    const geo = resolveGeometry(700);
    const slot = nextChartSlot([{ type: 'chart', x: geo.leftX, y: REPORT.startY, width: geo.chartW, height: REPORT.chartH } as CanvasBlock], { width: 700 });
    expect(slot).toEqual({ x: geo.leftX, y: REPORT.startY + REPORT.chartH + REPORT.gapY });
  });
});

describe('nextFullWidthSlot', () => {
  it('通栏块放在两列最深底部之下', () => {
    const left = chart(20, REPORT.startY);
    const right = chart(520, 400); // 底 720 更深
    const slot = nextFullWidthSlot([left, right]);
    expect(slot).toEqual({ x: 20, y: 720 + REPORT.gapY });
  });
});

describe('applyReportLayout', () => {
  it('h1 通栏 + 图表双列 + 叙事通栏紧跟', () => {
    const blocks: CanvasBlock[] = [h1(), chart(), chart(), text()];
    const out = applyReportLayout(blocks) as Array<Record<string, any>>;

    // h1 通栏：从 startY 起步，无顶部 180 死区
    expect(out[0]).toMatchObject({ x: 20, y: REPORT.startY, width: GEO.fullW });
    const h1Bottom = REPORT.startY + estimateBlockHeight(h1());

    // 两张图双列同行
    expect(out[1]).toMatchObject({ x: 20, y: h1Bottom + REPORT.gapY, width: GEO.chartW, height: REPORT.chartH });
    expect(out[2]).toMatchObject({ x: 520, y: h1Bottom + REPORT.gapY, width: GEO.chartW, height: REPORT.chartH });
    const chartsBottom = h1Bottom + REPORT.gapY + REPORT.chartH;

    // 叙事通栏紧跟图表下方（重置双列游标）
    expect(out[3]).toMatchObject({ x: 20, y: chartsBottom + REPORT.gapY, width: GEO.fullW });
  });

  it('第三张图换行到左列', () => {
    const out = applyReportLayout([chart(), chart(), chart()]) as Array<Record<string, any>>;
    expect(out[0]).toMatchObject({ x: 20, y: REPORT.startY });
    expect(out[1]).toMatchObject({ x: 520, y: REPORT.startY });
    expect(out[2]).toMatchObject({ x: 20, y: REPORT.startY + REPORT.chartH + REPORT.gapY });
  });

  it('窄画布（单列）：图表通栏，不溢出画布实宽', () => {
    const geo = resolveGeometry(700);
    const out = applyReportLayout([chart(), chart()], { width: 700 }) as Array<Record<string, any>>;
    expect(out[0]).toMatchObject({ x: geo.leftX, y: REPORT.startY, width: geo.fullW });
    expect(out[1].x).toBe(geo.leftX); // 第二张也落左列（下一行）
    expect(Math.max(...out.map((b) => b.x + b.width))).toBeLessThanOrEqual(700);
  });

  it('宽画布（双列）：图表撑满两列，右侧不留空白带', () => {
    const geo = resolveGeometry(1440);
    const out = applyReportLayout([chart(), chart()], { width: 1440 }) as Array<Record<string, any>>;
    const rightEdge = Math.max(...out.map((b) => b.x + b.width));
    expect(rightEdge).toBe(geo.rightX + geo.chartW); // 右列右边缘 = 通栏右边缘
    expect(rightEdge).toBeLessThanOrEqual(1440);
  });

  it('不改变块的数量与内容', () => {
    const blocks: CanvasBlock[] = [h1('T'), chart(), text('n')];
    const out = applyReportLayout(blocks);
    expect(out).toHaveLength(3);
    expect(out[0]).toMatchObject({ type: 'h1', content: 'T' });
    expect(out[2]).toMatchObject({ type: 'text', content: 'n' });
  });

  it('带显式高度的叙事块：整理后被重置为自适应高度，后续块紧贴其真实高度（回归：被压住重叠）', () => {
    const tall: CanvasBlock = { type: 'h1', blockId: 'h1_a', content: '新标题', height: 250 } as CanvasBlock;
    const t: CanvasBlock = { type: 'text', blockId: 'text_a', content: '新文本块...' } as CanvasBlock;
    const out = applyReportLayout([tall, t], { width: 1200 }) as Array<Record<string, any>>;

    expect(out[0].y).toBe(REPORT.startY);
    // 固定 250 空框被清掉 → 由内容自适应
    expect(out[0].height).toBeUndefined();
    // 后续块的 y = 前块渲染高度 + 间距（严格按渲染高度推进，不留空隙也不重叠）
    const h1Rendered = estimateBlockHeight(out[0], resolveGeometry(1200));
    const textRendered = estimateBlockHeight(out[1], resolveGeometry(1200));
    expect(out[1].y).toBe(out[0].y + h1Rendered + REPORT.gapY);
    expect(out[1].y).toBeGreaterThanOrEqual(out[0].y + h1Rendered);
    expect(textRendered).toBeGreaterThan(0);
  });

  it('叙事块一律等宽通栏（不受历史宽度影响），图片保留原宽且不超通栏', () => {
    const geo = resolveGeometry(1200);
    const narr: CanvasBlock = { type: 'text', blockId: 'text_b', content: '段落', width: 500 } as CanvasBlock;
    const img: CanvasBlock = { type: 'image', src: 'x', width: 300, height: 200 } as CanvasBlock;
    const out = applyReportLayout([narr, img], { width: 1200 }) as Array<Record<string, any>>;

    expect(out[0]).toMatchObject({ x: geo.leftX, width: geo.fullW });
    expect(out[1]).toMatchObject({ width: 300, height: 200 }); // 图片保留宽度与高度
  });

  it('整理后块之间零重叠（混合高度场景）', () => {
    const blocks: CanvasBlock[] = [
      { type: 'h1', blockId: 'h1_x', content: '标题', height: 250 } as CanvasBlock,
      text('段落一'),
      chart(),
      { type: 'text', blockId: 'text_y', content: '段落二', height: 400 } as CanvasBlock,
      chart(),
    ];
    const out = applyReportLayout(blocks, { width: 1200 }) as Array<Record<string, any>>;
    // 用「渲染高度」（显式高度优先，否则内容估算）验证两两不重叠
    let lastFullBottom = -1;
    for (const b of out) {
      if (b.type === 'chart') continue;
      expect(b.y).toBeGreaterThan(lastFullBottom);
      lastFullBottom = b.y + estimateBlockHeight(b, resolveGeometry(1200));
    }
  });
});

describe('assignBlockLabels', () => {
  it('按坐标分行：同行左右编号，下一行换字母（与后端同规则）', () => {
    const blocks: CanvasBlock[] = [
      { type: 'h1', content: '标题', x: 20, y: 32 } as CanvasBlock,                       // 第 1 行
      { type: 'chart', blockId: 'c1', x: 20, y: 120 } as CanvasBlock,                      // 第 2 行左
      { type: 'chart', blockId: 'c2', x: 520, y: 124 } as CanvasBlock,                     // 第 2 行右（y 差 4 < 10 → 同行）
      { type: 'text', content: '结论', x: 20, y: 500 } as CanvasBlock,                     // 第 3 行
    ];
    expect(assignBlockLabels(blocks)).toEqual(['A1', 'B1', 'B2', 'C1']);
  });

  it('无序数组也按坐标排序编号（不依赖数组顺序）', () => {
    const ordered: CanvasBlock[] = [
      { type: 'h1', content: '标题', x: 20, y: 32 } as CanvasBlock,
      { type: 'chart', blockId: 'c1', x: 20, y: 200 } as CanvasBlock,
      { type: 'chart', blockId: 'c2', x: 520, y: 200 } as CanvasBlock,
    ];
    const shuffled = [ordered[2], ordered[0], ordered[1]];
    // 下标 0 是右列图（B2）、下标 1 是标题（A1）、下标 2 是左列图（B1）
    expect(assignBlockLabels(shuffled)).toEqual(['B2', 'A1', 'B1']);
  });

  it('无坐标的块排在最后继续累计编号', () => {
    const blocks: CanvasBlock[] = [
      { type: 'h1', content: '标题', x: 20, y: 32 } as CanvasBlock,
      { type: 'text', content: '未定位' } as CanvasBlock,
    ];
    expect(assignBlockLabels(blocks)).toEqual(['A1', 'B1']);
  });

  it('空数组返回空编号', () => {
    expect(assignBlockLabels([])).toEqual([]);
  });
});