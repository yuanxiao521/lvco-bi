/**
 * 画布切换时的会话隔离回归测试。
 *
 * 复现的两个"历史串台"漏洞：
 * 1) resetForCanvas 没清 canvasSessions → onCanvasReady 拿上一个画布的会话列表当本画布的，
 *    采纳它的会话 id 并加载其历史 → 上一个画布的会话历史显示到本画布上。
 * 2) 从别的路由/画布列表重新进入时，组件 prev 引用为 null，被误判成"同一画布"而不重置。
 *    （第 2 条由 AIAssistant 判定，这里守住 store 侧的判定输入 getActiveCanvasId）
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../api/ai', () => ({
  listCanvasSessions: vi.fn(async () => []),
  listMessages: vi.fn(async () => []),
}));

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

import { useCanvasAssistantStore, getActiveCanvasId } from '../../stores/canvasAssistantStore';

const ctxFor = (canvasId: string | null) => ({
  canvasId,
  datasourceId: null,
  fieldMeta: [],
  canvasBlocks: [],
  allDatasources: [],
} as never);

describe('画布切换的会话隔离', () => {
  beforeEach(() => {
    useCanvasAssistantStore.setState({
      curSessionId: 'sess-A',
      sessionsLoaded: true,
      canvasSessions: [{ id: 'sess-A', canvasId: 'canvas-A', entry: 'canvas' } as never],
      messages: [{ id: 'm1', role: 'user', content: 'A 画布的历史' } as never],
    });
  });

  it('切到另一张画布后，上一画布的会话列表与会话 id 都被清掉', async () => {
    useCanvasAssistantStore.getState().resetForCanvas(ctxFor('canvas-B'));

    const s = useCanvasAssistantStore.getState();
    expect(s.curSessionId).toBeNull();
    expect(s.canvasSessions).toEqual([]);   // 关键：不清空会被 onCanvasReady 采纳 → 历史串台
    expect(s.sessionsLoaded).toBe(false);
    expect(s.messages.some((m) => m.content === 'A 画布的历史')).toBe(false);
    expect(getActiveCanvasId()).toBe('canvas-B');
  });

  it('会话列表为空时 onCanvasReady 不加载任何历史（等本画布的列表到位）', async () => {
    useCanvasAssistantStore.getState().resetForCanvas(ctxFor('canvas-B'));
    await useCanvasAssistantStore.getState().onCanvasReady('canvas-B');

    const s = useCanvasAssistantStore.getState();
    expect(s.sessionsLoaded).toBe(false); // 未拿到本画布会话前不得标记"已加载"
    expect(s.messages.some((m) => m.content === 'A 画布的历史')).toBe(false);
  });

  it('归属画布判定：切换后 store 归属新画布（供组件区分"重挂载"与"切画布"）', () => {
    // 归属是模块级状态，逐步切换以断言它跟随画布变化
    useCanvasAssistantStore.getState().resetForCanvas(ctxFor('canvas-A'));
    expect(getActiveCanvasId()).toBe('canvas-A');

    useCanvasAssistantStore.getState().resetForCanvas(ctxFor('canvas-B'));
    expect(getActiveCanvasId()).toBe('canvas-B');

    // 回到原画布：归属判定指向 A，组件据此触发重置（而不是沿用 B 的历史）
    useCanvasAssistantStore.getState().resetForCanvas(ctxFor('canvas-A'));
    expect(getActiveCanvasId()).toBe('canvas-A');
  });
});