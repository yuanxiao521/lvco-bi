/**
 * 画布换数据源时的图表重映射测试。
 *
 * 契约：换源后图表块保留（标题/图型/位置不白费），但必须
 * ① 清空维度与度量（旧源字段在新源上必然不存在）
 * ② 丢弃旧源的查询结果（否则画布上会显示上一个源的数据 → 混源）
 * ③ 把配置指向新源
 */
import { describe, it, expect } from 'vitest';
import { remapChartsForDatasource } from '../../utils/canvasDatasource';
import type { CanvasBlock } from '../../types/canvas';
import type { ChartQueryConfig, QueryResult } from '../../types/chart';

const chartBlock = (blockId: string): CanvasBlock =>
  ({ type: 'chart', blockId, title: '订单趋势', chartType: 'line', x: 20, y: 120 } as CanvasBlock);
const textBlock: CanvasBlock = { type: 'text', content: '叙事' } as CanvasBlock;

const cfg = (datasourceId: string): ChartQueryConfig =>
  ({ dimensions: ['order_date'], measures: [{ field: 'amount', agg: 'SUM' }], filters: [], chartType: 'line', limit: 20, datasourceId } as ChartQueryConfig);
const res: QueryResult = { columns: ['order_date', 'amount'], rows: [{ order_date: '2024-01-01', amount: 10 }], chartType: 'line', queryTimeMs: 5 };

describe('remapChartsForDatasource', () => {
  it('图表块：清空维度度量、丢弃旧结果、指向新源，并报出受影响数量', () => {
    const out = remapChartsForDatasource(
      [chartBlock('c1'), chartBlock('c2'), textBlock],
      { c1: cfg('ds-old'), c2: cfg('ds-old') },
      { c1: res, c2: res },
      'ds-new',
    );

    expect(out.affected).toBe(2);
    expect(out.chartConfigs.c1).toMatchObject({
      dimensions: [], measures: [], filters: [], chartType: 'line', datasourceId: 'ds-new',
    });
    expect(out.chartConfigs.c2?.datasourceId).toBe('ds-new');
    // 旧源的数据必须丢弃（否则画布上显示的仍是老数据）
    expect(out.chartResults.c1).toBeUndefined();
    expect(out.chartResults.c2).toBeUndefined();
  });

  it('保留图表类型与条数上限（用户排版与图型不白费）', () => {
    const out = remapChartsForDatasource(
      [chartBlock('c1')],
      { c1: { ...cfg('ds-old'), chartType: 'donut', limit: 50 } },
      {},
      'ds-new',
    );
    expect(out.chartConfigs.c1).toMatchObject({ chartType: 'donut', limit: 50 });
  });

  it('没有图表块时 affected=0，且不改动原有配置', () => {
    const configs = { c1: cfg('ds-old') };
    const out = remapChartsForDatasource([textBlock], configs, { c1: res }, 'ds-new');
    expect(out.affected).toBe(0);
    expect(out.chartConfigs).toEqual(configs);
    expect(out.chartResults.c1).toBe(res);
  });

  it('不修改入参对象（纯函数）', () => {
    const configs = { c1: cfg('ds-old') };
    const results = { c1: res };
    remapChartsForDatasource([chartBlock('c1')], configs, results, 'ds-new');
    expect(configs.c1.datasourceId).toBe('ds-old');
    expect(configs.c1.dimensions).toEqual(['order_date']);
    expect(results.c1).toBe(res);
  });
});