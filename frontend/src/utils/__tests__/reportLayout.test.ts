import { describe, it, expect } from 'vitest';
import { REPORT, deriveCursor, nextChartSlot, nextFullWidthSlot, applyReportLayout } from '../../utils/reportLayout';
import type { CanvasBlock } from '../../types/canvas';

const chart = (x?: number, y?: number): CanvasBlock =>
  ({ type: 'chart', blockId: 'c1', x, y, width: REPORT.chartW, height: REPORT.chartH } as CanvasBlock);
const text = (content = '叙事', x?: number, y?: number): CanvasBlock =>
  ({ type: 'text', content, x, y, width: REPORT.fullW } as CanvasBlock);
const h1 = (content = '标题'): CanvasBlock => ({ type: 'h1', content } as CanvasBlock);

describe('deriveCursor', () => {
  it('空画布返回起点', () => {
    expect(deriveCursor([])).toEqual({ leftBottom: REPORT.startY, rightBottom: REPORT.startY });
  });

  it('通栏文本块把两列游标同时压到底部', () => {
    const t = text('x', 20, 100); // 底部 100+72=172 → 取 max(180,172)=180
    const c = deriveCursor([t]);
    expect(c.leftBottom).toBe(180);
    expect(c.rightBottom).toBe(180);
    const t2 = text('y', 20, 200); // 底部 272 > 180
    const c2 = deriveCursor([t2]);
    expect(c2.leftBottom).toBe(272);
    expect(c2.rightBottom).toBe(272);
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
    const left = chart(20, REPORT.startY); // 左底 500
    const right = chart(520, 400); // 右底 720
    const slot = nextChartSlot([left, right]);
    expect(slot).toEqual({ x: 20, y: 500 + REPORT.gapY });
  });
});

describe('nextFullWidthSlot', () => {
  it('通栏块放在两列最深底部之下', () => {
    const left = chart(20, REPORT.startY); // 底 500
    const right = chart(520, 400); // 底 720
    const slot = nextFullWidthSlot([left, right]);
    expect(slot).toEqual({ x: 20, y: 720 + REPORT.gapY });
  });
});

describe('applyReportLayout', () => {
  it('h1 通栏 + 图表双列 + 叙事通栏紧跟', () => {
    const blocks: CanvasBlock[] = [h1(), chart(), chart(), text()];
    const out = applyReportLayout(blocks) as Array<Record<string, any>>;

    // h1 通栏
    expect(out[0].x).toBe(20);
    expect(out[0].width).toBe(REPORT.fullW);
    const h1Bottom = 180 + 56;

    // 两张图双列同行
    expect(out[1]).toMatchObject({ x: 20, y: h1Bottom + REPORT.gapY, width: 480, height: 320 });
    expect(out[2]).toMatchObject({ x: 520, y: h1Bottom + REPORT.gapY, width: 480, height: 320 });
    const chartsBottom = h1Bottom + REPORT.gapY + 320;

    // 叙事通栏紧跟图表下方（重置双列游标）
    expect(out[3]).toMatchObject({ x: 20, y: chartsBottom + REPORT.gapY, width: REPORT.fullW });
  });

  it('第三张图换行到左列', () => {
    const out = applyReportLayout([chart(), chart(), chart()]) as Array<Record<string, any>>;
    expect(out[0]).toMatchObject({ x: 20, y: REPORT.startY });
    expect(out[1]).toMatchObject({ x: 520, y: REPORT.startY });
    expect(out[2]).toMatchObject({ x: 20, y: REPORT.startY + 320 + REPORT.gapY });
  });

  it('不改变块的数量与内容', () => {
    const blocks: CanvasBlock[] = [h1('T'), chart(), text('n')];
    const out = applyReportLayout(blocks);
    expect(out).toHaveLength(3);
    expect(out[0]).toMatchObject({ type: 'h1', content: 'T' });
    expect(out[2]).toMatchObject({ type: 'text', content: 'n' });
  });
});
